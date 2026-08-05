from __future__ import annotations

from datetime import date
from io import BytesIO
from pathlib import Path
import re
import tempfile
import unittest
from zipfile import ZIP_DEFLATED, ZipFile

from openpyxl import Workbook

from app.documents import (
    CsvEncodingError,
    CsvStructureError,
    NodeType,
    iter_document_nodes_from_bytes,
)
from app.lite.indexer import build_index_from_nodes
from app.lite.search import search_index


def _workbook_bytes(*, cached_formula: bool) -> bytes:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "预算明细"
    sheet.append(["部门", "日期", "完成率", "预算", "预算翻倍"])
    sheet.append(["研发部", date(2026, 8, 2), 0.125, 1234.5, "=D2*2"])
    sheet["C2"].number_format = "0.0%"
    sheet["D2"].number_format = "0.00"
    sheet["E2"].number_format = "0.00"
    sheet.merge_cells("A3:B3")
    sheet["A3"] = "合并说明"
    stream = BytesIO()
    workbook.save(stream)
    workbook.close()
    content = stream.getvalue()
    if not cached_formula:
        return content

    source = BytesIO(content)
    target = BytesIO()
    replaced = False
    with ZipFile(source, "r") as reader, ZipFile(
        target,
        "w",
        compression=ZIP_DEFLATED,
    ) as writer:
        for item in reader.infolist():
            payload = reader.read(item.filename)
            if item.filename == "xl/worksheets/sheet1.xml":
                payload, count = re.subn(
                    rb'(<c r="E2"[^>]*><f>[^<]+</f>)<v\s*/>(</c>)',
                    rb"\g<1><v>2469</v>\g<2>",
                    payload,
                    count=1,
                )
                replaced = count == 1
            writer.writestr(item, payload)
    if not replaced:
        raise AssertionError("Formula cache fixture could not be created.")
    return target.getvalue()


class P06AcceptanceTests(unittest.TestCase):
    def test_csv_detects_supported_encodings_and_delimiters(self) -> None:
        fixtures = (
            ("utf8.csv", "部门,金额\n研发部,100\n".encode("utf-8"), ",", "utf-8"),
            (
                "bom.csv",
                "部门;金额\n研发部;100\n".encode("utf-8-sig"),
                ";",
                "utf-8-sig",
            ),
            (
                "gb.csv",
                "部门\t金额\n研发部\t100\n".encode("gb18030"),
                "\t",
                "gb18030",
            ),
        )
        for filename, content, delimiter, encoding in fixtures:
            with self.subTest(filename=filename):
                nodes = list(
                    iter_document_nodes_from_bytes(filename, content)
                )
                self.assertEqual(nodes[0].metadata["columns"], ["部门", "金额"])
                self.assertEqual(nodes[0].source_anchor["delimiter"], delimiter)
                self.assertEqual(nodes[0].source_anchor["encoding"], encoding)
                self.assertEqual(nodes[1].source_anchor["row_numbers"], [2])

    def test_csv_requires_manual_encoding_when_detection_fails(self) -> None:
        content = b"name,value\ncaf\xe9,1\n"
        with self.assertRaises(CsvEncodingError):
            list(iter_document_nodes_from_bytes("latin.csv", content))

        nodes = list(
            iter_document_nodes_from_bytes(
                "latin.csv",
                content,
                csv_encoding="latin-1",
            )
        )
        self.assertIn("café", nodes[1].content)
        self.assertEqual(nodes[0].source_anchor["encoding"], "iso8859-1")

    def test_csv_rejects_inconsistent_column_counts_with_row_number(self) -> None:
        content = "部门,金额\n研发部,100\n销售部,80,extra\n".encode("utf-8")
        with self.assertRaises(CsvStructureError) as context:
            list(iter_document_nodes_from_bytes("broken.csv", content))
        self.assertEqual(context.exception.row_number, 3)
        self.assertEqual(context.exception.expected_columns, 2)
        self.assertEqual(context.exception.actual_columns, 3)

    def test_xlsx_preserves_merges_formats_and_cached_formulas(self) -> None:
        nodes = list(
            iter_document_nodes_from_bytes(
                "budget.xlsx",
                _workbook_bytes(cached_formula=True),
            )
        )
        workbook_summary = nodes[0]
        summary = nodes[1]
        row_group = nodes[2]

        self.assertEqual(
            workbook_summary.node_type,
            NodeType.WORKBOOK_SUMMARY,
        )
        self.assertIn("Excel 文件：budget.xlsx", workbook_summary.content)
        self.assertEqual(summary.node_type, NodeType.SHEET_SUMMARY)
        self.assertEqual(summary.parent_id, workbook_summary.node_id)
        self.assertEqual(summary.metadata["merged_ranges"], ["A3:B3"])
        self.assertIn("部门", summary.metadata["key_columns"])
        self.assertIn("完成率", summary.metadata["metric_columns"])
        self.assertIn("预算", summary.metadata["metric_columns"])
        self.assertIn("2026-08-02", row_group.content)
        self.assertIn("12.5%", row_group.content)
        self.assertIn("1234.50", row_group.content)
        self.assertIn("2469.00（公式 =D2*2）", row_group.content)
        formula = row_group.metadata["formula_cells"][0]
        self.assertEqual(formula["coordinate"], "E2")
        self.assertEqual(formula["cache_status"], "available")
        self.assertEqual(formula["cached_value"], "2469.00")
        self.assertEqual(row_group.source_anchor["sheet"], "预算明细")
        self.assertEqual(row_group.source_anchor["row_numbers"], [2, 3])

    def test_xlsx_formula_without_cache_is_not_treated_as_blank(self) -> None:
        nodes = list(
            iter_document_nodes_from_bytes(
                "budget.xlsx",
                _workbook_bytes(cached_formula=False),
            )
        )
        row_group = nodes[2]
        self.assertIn("[公式无缓存值: =D2*2]", row_group.content)
        self.assertEqual(row_group.metadata["missing_formula_cache_count"], 1)
        self.assertEqual(
            row_group.metadata["formula_cells"][0]["cache_status"],
            "missing",
        )

    def test_csv_and_xlsx_rows_are_searchable_with_structured_citations(self) -> None:
        csv_nodes = iter_document_nodes_from_bytes(
            "headcount.csv",
            "部门,人数\n研发部,37\n销售部,22\n".encode("utf-8"),
        )
        xlsx_nodes = iter_document_nodes_from_bytes(
            "budget.xlsx",
            _workbook_bytes(cached_formula=True),
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            index_dir = Path(temp_dir) / "index"
            build_index_from_nodes(
                (node for source in (csv_nodes, xlsx_nodes) for node in source),
                index_dir,
            )

            csv_result = search_index("研发部有多少人", index_dir, top_k=3)[0]
            xlsx_result = search_index("研发部预算翻倍是多少", index_dir, top_k=3)[0]

        self.assertEqual(csv_result["source_anchor"]["row_start"], 2)
        self.assertEqual(csv_result["source_anchor"]["column_end"], 2)
        self.assertEqual(xlsx_result["source_anchor"]["sheet"], "预算明细")
        self.assertIn(2, xlsx_result["source_anchor"]["row_numbers"])


if __name__ == "__main__":
    unittest.main()
