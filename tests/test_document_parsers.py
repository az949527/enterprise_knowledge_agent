from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

import fitz
from openpyxl import Workbook

from app.documents import (
    NodeType,
    ParserError,
    iter_document_nodes,
    iter_document_nodes_from_bytes,
)
from app.documents.table_display import (
    render_markdown_table,
    render_table_display,
)


class DocumentParserTests(unittest.TestCase):
    def test_text_parser_is_iterator_and_preserves_source_anchor(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "policy.md"
            path.write_text("# 请假制度\n\n提前提交申请。", encoding="utf-8")

            nodes = iter_document_nodes(
                path,
                source_path="hr/policy.md",
            )

            self.assertTrue(hasattr(nodes, "__next__"))
            result = list(nodes)
            self.assertEqual(len(result), 1)
            self.assertEqual(result[0].node_type, NodeType.TEXT)
            self.assertEqual(
                result[0].source_anchor["source_path"],
                "hr/policy.md",
            )

    def test_pdf_parser_emits_page_and_bbox_metadata(self) -> None:
        document = fitz.open()
        page = document.new_page(width=300, height=200)
        page.insert_text((30, 40), "Policy page content")
        content = document.tobytes()
        document.close()

        nodes = list(
            iter_document_nodes_from_bytes(
                "policy.pdf",
                content,
            )
        )

        self.assertEqual(len(nodes), 1)
        self.assertEqual(nodes[0].page_or_sheet, 1)
        self.assertIsNotNone(nodes[0].bbox)
        self.assertEqual(nodes[0].source_anchor["page"], 1)

    def test_pdf_parser_orders_two_columns_left_then_right(self) -> None:
        document = fitz.open()
        page = document.new_page(width=600, height=400)
        page.insert_text((330, 70), "Right first")
        page.insert_text((330, 110), "Right second")
        page.insert_text((40, 70), "Left first")
        page.insert_text((40, 110), "Left second")
        content = document.tobytes()
        document.close()

        nodes = list(iter_document_nodes_from_bytes("columns.pdf", content))
        text = "\n".join(node.content for node in nodes)

        self.assertLess(text.index("Left first"), text.index("Left second"))
        self.assertLess(text.index("Left second"), text.index("Right first"))
        self.assertLess(text.index("Right first"), text.index("Right second"))

    def test_pdf_parser_tracks_heading_and_excludes_table_from_text(self) -> None:
        document = fitz.open()
        page = document.new_page(width=400, height=300)
        page.insert_text((40, 45), "1 Policy Overview", fontsize=18)
        page.insert_text((40, 75), "The policy applies to all departments.")
        xs = [40, 180, 340]
        ys = [110, 150, 190]
        for x in xs:
            page.draw_line((x, ys[0]), (x, ys[-1]))
        for y in ys:
            page.draw_line((xs[0], y), (xs[-1], y))
        page.insert_text((55, 137), "Department")
        page.insert_text((195, 137), "Limit")
        page.insert_text((55, 177), "Research")
        page.insert_text((195, 177), "100")
        content = document.tobytes()
        document.close()

        nodes = list(iter_document_nodes_from_bytes("table.pdf", content))
        text_nodes = [node for node in nodes if node.node_type is NodeType.TEXT]
        table_nodes = [node for node in nodes if node.node_type is NodeType.TABLE]

        self.assertEqual(len(table_nodes), 1)
        self.assertEqual(table_nodes[0].metadata["headers"], ["Department", "Limit"])
        self.assertEqual(table_nodes[0].metadata["cells"][1], ["Research", "100"])
        self.assertEqual(table_nodes[0].page_or_sheet, 1)
        self.assertIsNotNone(table_nodes[0].bbox)
        self.assertIn("1 Policy Overview", table_nodes[0].section_path)
        self.assertNotIn(
            "Department",
            "\n".join(node.content for node in text_nodes),
        )

    def test_pdf_parser_removes_repeated_headers_footers_and_page_numbers(self) -> None:
        document = fitz.open()
        for page_number in range(1, 4):
            page = document.new_page(width=500, height=800)
            page.insert_text((40, 25), "Company Handbook 2026")
            page.insert_text((40, 120), f"Unique body content {page_number}")
            page.insert_text((210, 780), f"Page {page_number} of 3")
        content = document.tobytes()
        document.close()

        nodes = list(iter_document_nodes_from_bytes("handbook.pdf", content))
        text = "\n".join(node.content for node in nodes)

        self.assertNotIn("Company Handbook", text)
        self.assertNotIn("Page 1 of 3", text)
        self.assertIn("Unique body content 1", text)
        self.assertIn("Unique body content 3", text)

    def test_pdf_parser_reports_ocr_requirement_without_text_layer(self) -> None:
        document = fitz.open()
        page = document.new_page(width=300, height=200)
        page.draw_rect((30, 30, 270, 170), color=(0, 0, 0))
        content = document.tobytes()
        document.close()

        with self.assertRaisesRegex(ParserError, "OCR is required"):
            list(iter_document_nodes_from_bytes("scanned.pdf", content))

    def test_csv_parser_emits_summary_and_row_group_parent_relation(self) -> None:
        content = "部门,金额\n研发部,100\n销售部,80\n".encode("utf-8")

        nodes = list(
            iter_document_nodes_from_bytes(
                "budget.csv",
                content,
            )
        )

        self.assertEqual(
            [node.node_type for node in nodes],
            [NodeType.SHEET_SUMMARY, NodeType.ROW_GROUP],
        )
        self.assertEqual(nodes[1].parent_id, nodes[0].node_id)
        self.assertEqual(nodes[1].row_start, 2)
        self.assertEqual(nodes[1].row_end, 3)

    def test_xlsx_parser_emits_sheet_summary_and_rows(self) -> None:
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "预算"
        sheet.append(["部门", "金额"])
        sheet.append(["研发部", 100])
        stream = __import__("io").BytesIO()
        workbook.save(stream)
        workbook.close()

        nodes = list(
            iter_document_nodes_from_bytes(
                "budget.xlsx",
                stream.getvalue(),
            )
        )

        self.assertEqual(
            [node.node_type for node in nodes],
            [
                NodeType.WORKBOOK_SUMMARY,
                NodeType.SHEET_SUMMARY,
                NodeType.ROW_GROUP,
            ],
        )
        self.assertIn("Excel 文件：budget.xlsx", nodes[0].content)
        self.assertEqual(nodes[1].page_or_sheet, "预算")
        self.assertEqual(nodes[1].parent_id, nodes[0].node_id)
        self.assertEqual(nodes[2].parent_id, nodes[1].node_id)
        self.assertEqual(nodes[2].row_start, 2)


class TableDisplayTests(unittest.TestCase):
    def test_render_markdown_table_produces_header_and_separator(self) -> None:
        rendered = render_markdown_table(
            [
                ["城市", "标准"],
                ["一线城市", "500"],
                ["其他城市", "350"],
            ]
        )
        lines = rendered.splitlines()
        self.assertEqual(lines[0], "| 城市 | 标准 |")
        self.assertEqual(lines[1], "| --- | --- |")
        self.assertIn("一线城市", rendered)

    def test_render_table_display_reads_cells_from_metadata(self) -> None:
        record = {
            "node_type": "table",
            "metadata": {
                "cells": [
                    ["部门", "第二季度"],
                    ["研发部", "120"],
                ]
            },
        }
        self.assertIsNone(record.get("display_content"))
        display = render_table_display(record)
        self.assertIn("| 部门 | 第二季度 |", display)
        self.assertIn("| 研发部 | 120 |", display)

    def test_render_table_display_returns_none_without_cells(self) -> None:
        self.assertIsNone(render_table_display({"metadata": {}}))
        self.assertIsNone(render_table_display({}))

    def test_pdf_table_node_keeps_structure_and_lazy_display(self) -> None:
        document = fitz.open()
        page = document.new_page(width=500, height=320)
        for y in (60, 140, 250):
            page.draw_line((35, y), (465, y))
        for x, value in zip(
            (45, 145, 225, 305, 385),
            ("Sample", "Rc", "RMSEC", "Rp", "RMSEP"),
        ):
            page.insert_text((x, 122), value)
        for x, value in zip(
            (45, 145, 225, 305, 385),
            ("A", "0.98", "0.12", "0.96", "0.18"),
        ):
            page.insert_text((x, 180), value)
        content = document.tobytes()
        document.close()

        nodes = list(
            iter_document_nodes_from_bytes("three-line.pdf", content)
        )
        table = next(node for node in nodes if node.node_type is NodeType.TABLE)
        # 展示文本延迟生成：解析阶段只保存结构，不渲染 Markdown 表格。
        self.assertIsNone(table.display_content)
        self.assertIn(["A", "0.98", "0.12", "0.96", "0.18"], table.metadata["cells"])
        display = render_table_display(table.to_record())
        self.assertIsNotNone(display)
        self.assertIn("Sample", display)


if __name__ == "__main__":
    unittest.main()
