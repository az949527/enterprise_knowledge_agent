from __future__ import annotations

from io import BytesIO
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import fitz
from openpyxl import Workbook

from app.documents import DocumentNode
from app.lite import indexer
from app.lite.bm25_search import search_bm25_index


INDEX_FILES = (
    "nodes.jsonl",
    "parents.jsonl",
    "chunks.jsonl",
    "manifest.json",
)


class P0_3AcceptanceTests(unittest.TestCase):
    def test_mixed_format_pipeline_preserves_source_anchors(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source_dir = root / "documents"
            index_dir = root / "index"
            source_dir.mkdir()
            (source_dir / "policy.txt").write_text(
                "txtanchor employee travel policy",
                encoding="utf-8",
            )
            (source_dir / "handbook.md").write_text(
                "# Handbook\n\nmdanchor annual leave policy",
                encoding="utf-8",
            )
            self._write_pdf(source_dir / "manual.pdf", "pdfanchor safety manual")
            (source_dir / "budget.csv").write_text(
                "department,amount\ncsvanchor,100\n",
                encoding="utf-8",
            )
            self._write_xlsx(source_dir / "plan.xlsx", "xlsxanchor")

            stats = indexer.build_index(source_dir, index_dir)
            manifest = json.loads(
                (index_dir / "manifest.json").read_text(encoding="utf-8")
            )

            self.assertEqual(stats.file_count, 5)
            self.assertEqual(manifest["file_count"], 5)
            self.assertEqual(
                set(manifest["extensions"]),
                {".txt", ".md", ".pdf", ".csv", ".xlsx"},
            )
            for name in INDEX_FILES:
                self.assertTrue((index_dir / name).is_file(), name)

            pdf_result = search_bm25_index(
                "pdfanchor",
                index_dir,
                top_k=1,
            )[0]
            csv_result = search_bm25_index(
                "csvanchor",
                index_dir,
                top_k=1,
            )[0]
            xlsx_result = search_bm25_index(
                "xlsxanchor",
                index_dir,
                top_k=1,
            )[0]

            self.assertEqual(pdf_result["filename"], "manual.pdf")
            self.assertEqual(pdf_result["page_or_sheet"], 1)
            self.assertEqual(pdf_result["source_anchor"]["page"], 1)
            self.assertIsNotNone(pdf_result["bbox"])

            self.assertEqual(csv_result["filename"], "budget.csv")
            self.assertEqual(csv_result["row_start"], 2)
            self.assertEqual(csv_result["row_end"], 2)
            self.assertEqual(csv_result["source_anchor"]["column_start"], 1)

            self.assertEqual(xlsx_result["filename"], "plan.xlsx")
            self.assertEqual(xlsx_result["page_or_sheet"], "Plan")
            self.assertEqual(xlsx_result["source_anchor"]["sheet"], "Plan")
            self.assertEqual(xlsx_result["row_start"], 2)

    def test_old_index_format_requires_rebuild_when_listing(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            index_dir = Path(temp_dir)
            (index_dir / "manifest.json").write_text(
                json.dumps({"index_format_version": 1, "documents": []}),
                encoding="utf-8",
            )
            (index_dir / "chunks.jsonl").write_text("{}\n", encoding="utf-8")

            with self.assertRaisesRegex(indexer.IndexFormatError, "重建索引"):
                indexer.list_index_documents(index_dir)

    def test_parser_failure_keeps_complete_old_index(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            index_dir = Path(temp_dir)
            self._write_initial_index(index_dir)
            before = self._snapshot(index_dir)

            def failing_nodes():
                yield self._node("new.txt", "new content")
                raise RuntimeError("simulated parser interruption")

            with self.assertRaisesRegex(RuntimeError, "parser interruption"):
                indexer.write_node_index(
                    failing_nodes(),
                    source_label="test",
                    index_dir=index_dir,
                    chunk_size=900,
                    chunk_overlap=120,
                )

            self.assertEqual(self._snapshot(index_dir), before)
            self.assertFalse(list(index_dir.glob("*.tmp")))

    def test_commit_failure_rolls_back_all_index_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            index_dir = Path(temp_dir)
            self._write_initial_index(index_dir)
            before = self._snapshot(index_dir)
            real_replace = indexer._replace_index_file
            calls = 0

            def interrupted_replace(source: Path, target: Path) -> None:
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError("simulated commit interruption")
                real_replace(source, target)

            with (
                patch.object(
                    indexer,
                    "_replace_index_file",
                    side_effect=interrupted_replace,
                ),
                self.assertRaisesRegex(OSError, "commit interruption"),
            ):
                indexer.write_node_index(
                    [self._node("replacement.txt", "replacement content")],
                    source_label="test",
                    index_dir=index_dir,
                    chunk_size=900,
                    chunk_overlap=120,
                )

            self.assertEqual(self._snapshot(index_dir), before)
            self.assertFalse(list(index_dir.glob("*.tmp")))
            self.assertFalse(list(index_dir.glob("*.bak")))

    def test_append_streams_existing_nodes_without_read_nodes_list(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            index_dir = Path(temp_dir)
            self._write_initial_index(index_dir)

            with patch.object(
                indexer,
                "read_nodes",
                side_effect=AssertionError("append must stream existing nodes"),
            ):
                stats = indexer.build_index_from_nodes(
                    [self._node("second.txt", "second document")],
                    index_dir,
                )

            self.assertEqual(stats.file_count, 2)
            self.assertEqual(stats.added_count, 1)

    def test_corrupt_jsonl_has_rebuild_diagnostic(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            index_dir = Path(temp_dir)
            self._write_initial_index(index_dir)
            (index_dir / "nodes.jsonl").write_text(
                "{not-json}\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(indexer.IndexFormatError, "重建索引"):
                indexer.read_nodes(index_dir)

    def test_reindex_archives_incompatible_index(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            index_dir = root / "index"
            index_dir.mkdir()
            (index_dir / "manifest.json").write_text(
                json.dumps({"index_format_version": 2}),
                encoding="utf-8",
            )
            for name in ("nodes.jsonl", "parents.jsonl", "chunks.jsonl"):
                (index_dir / name).write_text("", encoding="utf-8")

            stats = indexer.build_index_from_nodes(
                [self._node("replacement.txt", "replacement content")],
                index_dir,
            )

            archived = list(root.glob("index.old_incompatible_*"))
            current = json.loads(
                (index_dir / "manifest.json").read_text(encoding="utf-8")
            )
            legacy = json.loads(
                (archived[0] / "manifest.json").read_text(encoding="utf-8")
            )
            self.assertEqual(stats.file_count, 1)
            self.assertEqual(len(archived), 1)
            self.assertEqual(
                current["index_format_version"],
                indexer.INDEX_FORMAT_VERSION,
            )
            self.assertEqual(legacy["index_format_version"], 2)

    @staticmethod
    def _write_pdf(path: Path, text: str) -> None:
        document = fitz.open()
        page = document.new_page(width=300, height=200)
        page.insert_text((30, 40), text)
        document.save(path)
        document.close()

    @staticmethod
    def _write_xlsx(path: Path, value: str) -> None:
        workbook = Workbook()
        sheet = workbook.active
        sheet.title = "Plan"
        sheet.append(["department", "amount"])
        sheet.append([value, 200])
        stream = BytesIO()
        workbook.save(stream)
        workbook.close()
        path.write_bytes(stream.getvalue())

    @staticmethod
    def _node(filename: str, content: str) -> DocumentNode:
        return DocumentNode(
            document_id=f"doc_{filename.replace('.', '_')}",
            content=content,
            parser_version="acceptance_parser_v1",
            source_anchor={"source_path": filename},
            metadata={"filename": filename},
        )

    def _write_initial_index(self, index_dir: Path) -> None:
        indexer.write_node_index(
            [self._node("original.txt", "original content")],
            source_label="test",
            index_dir=index_dir,
            chunk_size=900,
            chunk_overlap=120,
        )

    @staticmethod
    def _snapshot(index_dir: Path) -> dict[str, bytes]:
        return {
            name: (index_dir / name).read_bytes()
            for name in INDEX_FILES
        }


if __name__ == "__main__":
    unittest.main()
