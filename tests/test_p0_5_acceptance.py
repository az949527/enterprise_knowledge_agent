from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace

import fitz

from app.documents import NodeType, iter_document_nodes_from_bytes
from app.documents.pdf_parser import _combine_header_rows, iter_pdf_document_nodes
from app.lite.bm25_search import search_bm25_index
from app.lite.indexer import build_index, read_chunks


class P0_5AcceptanceTests(unittest.TestCase):
    def test_heading_rules_exclude_noise_and_keep_real_heading(self) -> None:
        document = fitz.open()
        page = document.new_page(width=620, height=760)
        page.insert_text((40, 45), "2026", fontsize=18)
        page.insert_text((40, 80), "RMSEP = sqrt(error / n)", fontsize=18)
        page.insert_text((40, 115), "doi:10.1000/example.2026.1", fontsize=18)
        page.insert_text((40, 150), "Figure 2 Prediction results", fontsize=18)
        page.insert_text(
            (40, 185),
            "This sentence is deliberately too long to be treated as a section heading.",
            fontsize=18,
        )
        page.insert_text((40, 235), "2 Methods", fontsize=18)
        page.insert_text(
            (40, 275),
            "The method description contains enough ordinary body text for font sizing.",
        )
        content = document.tobytes()
        document.close()

        nodes = list(iter_document_nodes_from_bytes("headings.pdf", content))
        section_values = {
            value
            for node in nodes
            for value in node.section_path
        }

        self.assertIn("2 Methods", section_values)
        self.assertNotIn("2026", section_values)
        self.assertFalse(any("doi:" in value.casefold() for value in section_values))
        self.assertFalse(any(value.startswith("Figure 2") for value in section_values))
        self.assertFalse(any(value.startswith("This sentence") for value in section_values))

    def test_three_column_reading_order(self) -> None:
        document = fitz.open()
        page = document.new_page(width=660, height=400)
        for x, prefix in ((40, "Left"), (240, "Middle"), (440, "Right")):
            page.insert_text((x, 80), f"{prefix} first")
            page.insert_text((x, 120), f"{prefix} second")
        content = document.tobytes()
        document.close()

        nodes = list(iter_document_nodes_from_bytes("three-columns.pdf", content))
        text = "\n".join(node.content for node in nodes)

        expected = (
            "Left first",
            "Left second",
            "Middle first",
            "Middle second",
            "Right first",
            "Right second",
        )
        offsets = [text.index(value) for value in expected]
        self.assertEqual(offsets, sorted(offsets))

    def test_low_confidence_columns_keep_pymupdf_source_order(self) -> None:
        document = fitz.open()
        page = document.new_page(width=600, height=300)
        page.insert_text((330, 80), "Right source first")
        page.insert_text((40, 80), "Left source second")
        content = document.tobytes()
        document.close()

        nodes = list(iter_document_nodes_from_bytes("uncertain-columns.pdf", content))
        text = "\n".join(node.content for node in nodes)

        self.assertLess(text.index("Right source first"), text.index("Left source second"))

    def test_three_line_table_and_multilevel_headers(self) -> None:
        document = fitz.open()
        page = document.new_page(width=500, height=320)
        for y in (60, 140, 250):
            page.draw_line((35, y), (465, y))
        page.insert_text((145, 88), "Training")
        page.insert_text((305, 88), "Prediction")
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

        nodes = list(iter_document_nodes_from_bytes("three-line.pdf", content))
        table = next(node for node in nodes if node.node_type is NodeType.TABLE)

        self.assertEqual(table.metadata["detection_method"], "three_line")
        self.assertEqual(
            table.metadata["headers"],
            [
                "Sample",
                "Training.Rc",
                "Training.RMSEC",
                "Prediction.Rp",
                "Prediction.RMSEP",
            ],
        )
        self.assertIn(["A", "0.98", "0.12", "0.96", "0.18"], table.metadata["cells"])
        self.assertEqual(
            _combine_header_rows(
                [
                    ["", "训练集", "", "预测集", ""],
                    ["样品", "Rc", "RMSEC", "Rp", "RMSEP"],
                ]
            ),
            ["样品", "训练集.Rc", "训练集.RMSEC", "预测集.Rp", "预测集.RMSEP"],
        )

    def test_formula_lines_are_one_node_and_not_headings(self) -> None:
        document = fitz.open()
        page = document.new_page(width=500, height=320)
        page.insert_text((60, 80), "RMSEP =")
        page.insert_text((90, 105), "sqrt(sum(error^2) / n)")
        page.insert_text((40, 165), "Formula explanation appears in body text.")
        content = document.tobytes()
        document.close()

        nodes = list(iter_document_nodes_from_bytes("formula.pdf", content))
        formula_nodes = [
            node
            for node in nodes
            if node.metadata.get("content_kind") == "formula"
        ]

        self.assertEqual(len(formula_nodes), 1)
        self.assertIn("RMSEP =", formula_nodes[0].content)
        self.assertIn("sqrt(sum(error^2) / n)", formula_nodes[0].content)
        self.assertNotIn("RMSEP =", formula_nodes[0].section_path)

    def test_figure_node_binds_caption_bbox_and_nearby_text(self) -> None:
        document = fitz.open()
        page = document.new_page(width=500, height=400)
        page.insert_text((40, 55), "The prediction trend is discussed near the chart.")
        page.draw_rect((70, 90, 430, 250))
        page.insert_text((150, 280), "Figure 3 Prediction trend")
        content = document.tobytes()
        document.close()

        nodes = list(iter_document_nodes_from_bytes("figure.pdf", content))
        figure = next(node for node in nodes if node.node_type is NodeType.FIGURE)

        self.assertEqual(figure.metadata["caption"], "Figure 3 Prediction trend")
        self.assertIn("prediction trend is discussed", " ".join(figure.metadata["nearby_text"]))
        self.assertEqual(len(figure.source_anchor["bbox"]), 4)
        self.assertEqual(len(figure.source_anchor["caption_bbox"]), 4)
        self.assertEqual(len(figure.source_anchor["visual_bbox"]), 4)

    def test_cross_page_tables_merge_and_remove_repeated_header(self) -> None:
        document = fitz.open()
        first = document.new_page(width=420, height=800)
        self._draw_table(
            first,
            xs=(40, 210, 380),
            ys=(620, 680, 760),
            rows=(("Department", "Budget"), ("Research", "100")),
        )
        second = document.new_page(width=420, height=800)
        self._draw_table(
            second,
            xs=(40, 210, 380),
            ys=(40, 100, 160),
            rows=(("Department", "Budget"), ("Sales", "80")),
        )
        content = document.tobytes()
        document.close()

        nodes = list(iter_document_nodes_from_bytes("cross-page-table.pdf", content))
        tables = [node for node in nodes if node.node_type is NodeType.TABLE]

        self.assertEqual(len(tables), 1)
        self.assertTrue(tables[0].metadata["cross_page"])
        self.assertEqual(tables[0].metadata["page_start"], 1)
        self.assertEqual(tables[0].metadata["page_end"], 2)
        self.assertEqual(
            tables[0].metadata["cells"],
            [["Department", "Budget"], ["Research", "100"], ["Sales", "80"]],
        )

    def test_layout_scan_is_reused_and_plain_page_skips_find_tables(self) -> None:
        class FakePage:
            def __init__(self) -> None:
                self.rect = SimpleNamespace(width=500.0, height=700.0)
                self.get_text_calls = 0
                self.find_tables_calls = 0

            def get_text(self, _kind: str, *, sort: bool = False) -> dict:
                self.get_text_calls += 1
                return {
                    "blocks": [
                        {
                            "type": 0,
                            "lines": [
                                {
                                    "bbox": (40, 80, 350, 95),
                                    "spans": [
                                        {
                                            "text": "Ordinary paragraph without tabular layout.",
                                            "size": 11,
                                            "flags": 0,
                                            "font": "Helvetica",
                                        }
                                    ],
                                }
                            ],
                        }
                    ]
                }

            def get_drawings(self) -> list:
                return []

            def get_images(self, *, full: bool = False) -> list:
                return []

            def find_tables(self) -> None:
                self.find_tables_calls += 1
                raise AssertionError("find_tables must not run for a plain page")

        class FakeDocument:
            def __init__(self, page: FakePage) -> None:
                self.page = page

            def __len__(self) -> int:
                return 1

            def load_page(self, _index: int) -> FakePage:
                return self.page

        page = FakePage()
        nodes = list(
            iter_pdf_document_nodes(
                FakeDocument(page),
                document_id="plain",
                source_path="plain.pdf",
            )
        )

        self.assertEqual(len(nodes), 1)
        self.assertEqual(page.get_text_calls, 1)
        self.assertEqual(page.find_tables_calls, 0)

    def test_real_pdf_v3_closeout_fixture(self) -> None:
        pdf_candidates = sorted(Path("data/documents").glob("*.pdf"))
        if not pdf_candidates:
            self.skipTest("Real PDF closeout fixture is not available.")

        nodes = list(
            iter_document_nodes_from_bytes(
                pdf_candidates[0].name,
                pdf_candidates[0].read_bytes(),
            )
        )
        tables = [node for node in nodes if node.node_type is NodeType.TABLE]
        figures = [node for node in nodes if node.node_type is NodeType.FIGURE]
        formulas = [
            node
            for node in nodes
            if node.metadata.get("content_kind") == "formula"
        ]
        headings = {
            node.metadata.get("heading")
            for node in nodes
            if node.metadata.get("heading")
        }

        self.assertEqual(len(tables), 5)
        self.assertEqual(
            sum(bool(node.metadata.get("cross_page")) for node in tables),
            2,
        )
        self.assertEqual(len(figures), 8)
        self.assertTrue(all(node.metadata["visual_kind"] == "image" for node in figures))
        self.assertEqual(len(formulas), 3)
        self.assertNotIn(
            "1.载物板；2.傅里叶近红外光谱仪；3.计算机",
            headings,
        )
        self.assertNotIn(
            "pickling based on near-infrared spectroscopy \uf020",
            headings,
        )
        table5 = next(node for node in tables if node.page_or_sheet == 5)
        table_text = "\n".join("\t".join(row) for row in table5.metadata["cells"])
        self.assertIn("蛋黄氯化钠浓度(%)", table_text)

    def test_section_path_continues_across_pages(self) -> None:
        document = fitz.open()
        first = document.new_page(width=500, height=700)
        first.insert_text((40, 60), "1 Security Response", fontsize=18)
        first.insert_text((40, 100), "The response starts with incident triage.")
        second = document.new_page(width=500, height=700)
        second.insert_text(
            (40, 100),
            "The same response continues with containment and recovery.",
        )
        content = document.tobytes()
        document.close()

        nodes = list(iter_document_nodes_from_bytes("cross-page.pdf", content))
        second_page = next(node for node in nodes if node.page_or_sheet == 2)

        self.assertIn("1 Security Response", second_page.section_path)
        self.assertIn("containment and recovery", second_page.content)

    def test_pdf_table_is_independent_and_retrievable_with_anchor(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source_dir = root / "documents"
            index_dir = root / "index"
            source_dir.mkdir()
            pdf_path = source_dir / "budget-table.pdf"
            self._write_budget_table(pdf_path)

            build_index(source_dir, index_dir)
            chunks = read_chunks(index_dir)
            table_chunks = [
                chunk
                for chunk in chunks
                if chunk.get("node_type") == NodeType.TABLE.value
            ]
            text_chunks = [
                chunk
                for chunk in chunks
                if chunk.get("node_type") == NodeType.TEXT.value
            ]
            results = search_bm25_index(
                "Research department budget 100",
                index_dir,
                top_k=3,
            )

            self.assertEqual(len(table_chunks), 1)
            self.assertNotIn(
                "Research",
                "\n".join(chunk["content"] for chunk in text_chunks),
            )
            self.assertEqual(results[0]["node_type"], NodeType.TABLE.value)
            self.assertEqual(results[0]["page_or_sheet"], 1)
            self.assertEqual(results[0]["source_anchor"]["page"], 1)
            self.assertEqual(len(results[0]["source_anchor"]["bbox"]), 4)
            self.assertEqual(
                results[0]["metadata"]["headers"],
                ["Department", "Budget"],
            )

    @staticmethod
    def _write_budget_table(path: Path) -> None:
        document = fitz.open()
        page = document.new_page(width=420, height=320)
        page.insert_text((40, 45), "2 Department Budget", fontsize=18)
        page.insert_text((40, 75), "Approved budget values are listed below.")
        xs = [40, 210, 370]
        ys = [110, 150, 190, 230]
        for x in xs:
            page.draw_line((x, ys[0]), (x, ys[-1]))
        for y in ys:
            page.draw_line((xs[0], y), (xs[-1], y))
        page.insert_text((55, 137), "Department")
        page.insert_text((225, 137), "Budget")
        page.insert_text((55, 177), "Research")
        page.insert_text((225, 177), "100")
        page.insert_text((55, 217), "Sales")
        page.insert_text((225, 217), "80")
        document.save(path)
        document.close()

    @staticmethod
    def _draw_table(
        page: fitz.Page,
        *,
        xs: tuple[int, ...],
        ys: tuple[int, ...],
        rows: tuple[tuple[str, ...], ...],
    ) -> None:
        for x in xs:
            page.draw_line((x, ys[0]), (x, ys[-1]))
        for y in ys:
            page.draw_line((xs[0], y), (xs[-1], y))
        for row_index, row in enumerate(rows):
            baseline = ys[row_index] + 37
            for column_index, value in enumerate(row):
                page.insert_text((xs[column_index] + 15, baseline), value)


if __name__ == "__main__":
    unittest.main()
