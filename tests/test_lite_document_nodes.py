from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from app.documents import DocumentNode, NodeType
from app.lite.bm25_search import search_bm25_index
from app.lite.indexer import (
    INDEX_FORMAT_VERSION,
    IndexFormatError,
    LITE_PARSER_VERSION,
    build_index_from_nodes,
    chunk_structure,
    extract_document_nodes,
    extract_text,
    load_parent_content,
    read_chunks,
    read_nodes,
    read_parents,
    ensure_index_format,
    write_node_index,
)


class LiteDocumentNodeTests(unittest.TestCase):
    def test_text_extractor_returns_document_node_and_legacy_text(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "policy.md"
            path.write_text("员工应在三十天内提交报销。", encoding="utf-8")

            nodes = extract_document_nodes(
                path,
                source_path="finance/policy.md",
            )

            self.assertEqual(len(nodes), 1)
            self.assertEqual(nodes[0].node_type, NodeType.TEXT)
            self.assertEqual(nodes[0].parser_version, LITE_PARSER_VERSION)
            self.assertEqual(
                nodes[0].source_anchor["source_path"],
                "finance/policy.md",
            )
            self.assertEqual(extract_text(path), nodes[0].content)

    def test_node_lineage_is_saved_and_returned_by_search(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            index_dir = Path(temp_dir)
            node = DocumentNode(
                document_id="doc_policy",
                content="员工应在差旅结束后三十天内提交费用报销申请。",
                parser_version="test_parser_v1",
                node_type=NodeType.TEXT,
                section_path=("财务制度", "费用报销"),
                source_anchor={
                    "source_path": "finance/policy.md",
                    "section": "费用报销",
                },
                metadata={"filename": "policy.md", "file_type": ".md"},
            )

            stats = write_node_index(
                [node],
                source_label="test",
                index_dir=index_dir,
                chunk_size=900,
                chunk_overlap=120,
            )
            chunks = read_chunks(index_dir)
            result = search_bm25_index("差旅费用多久报销", index_dir, top_k=1)[0]
            manifest = json.loads(
                (index_dir / "manifest.json").read_text(encoding="utf-8")
            )

            self.assertEqual(stats.file_count, 1)
            self.assertEqual(chunks[0]["node_id"], node.node_id)
            self.assertEqual(chunks[0]["document_id"], node.document_id)
            self.assertEqual(chunks[0]["display_content"], None)
            self.assertEqual(len(read_nodes(index_dir)), 1)
            self.assertEqual(len(read_parents(index_dir)), 1)
            self.assertEqual(
                load_parent_content(index_dir, node.node_id)["content"],
                node.content,
            )
            self.assertEqual(result["node_id"], node.node_id)
            self.assertEqual(result["section_path"], ["财务制度", "费用报销"])
            self.assertEqual(
                result["source_anchor"]["section"],
                "费用报销",
            )
            self.assertEqual(manifest["document_node_schema_version"], 1)
            self.assertEqual(manifest["index_format_version"], 3)
            self.assertEqual(manifest["nodes_file"], "nodes.jsonl")
            self.assertEqual(manifest["parents_file"], "parents.jsonl")
            self.assertEqual(manifest["documents"][0]["node_count"], 1)

    def test_display_content_is_saved_once_for_split_node(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            node = DocumentNode(
                document_id="doc_table",
                content="第一行结构化数据\n\n第二行结构化数据",
                display_content="| 列名 |\n|---|\n| 第一行 |\n| 第二行 |",
                parser_version="table_parser_v1",
                node_type=NodeType.TABLE,
                source_anchor={"source_path": "table.pdf", "page": 1},
            )

            write_node_index(
                [node],
                source_label="test",
                index_dir=temp_dir,
                chunk_size=10,
                chunk_overlap=0,
            )
            chunks = read_chunks(temp_dir)

            self.assertGreater(len(chunks), 1)
            self.assertEqual(chunks[0]["display_content"], node.display_content)
            self.assertTrue(
                all(
                    chunk["display_content"] is None
                    for chunk in chunks[1:]
                )
            )

    def test_multiple_nodes_follow_sequence_and_share_document(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            later = DocumentNode(
                document_id="doc_multi",
                content="第二页内容",
                parser_version="pdf_parser_v1",
                sequence=1,
                page_or_sheet=2,
                source_anchor={"source_path": "multi.pdf", "page": 2},
            )
            earlier = DocumentNode(
                document_id="doc_multi",
                content="第一页内容",
                parser_version="pdf_parser_v1",
                sequence=0,
                page_or_sheet=1,
                source_anchor={"source_path": "multi.pdf", "page": 1},
            )

            stats = write_node_index(
                [later, earlier],
                source_label="test",
                index_dir=temp_dir,
                chunk_size=900,
                chunk_overlap=120,
            )
            chunks = read_chunks(temp_dir)

            self.assertEqual([chunk["content"] for chunk in chunks], [
                "第一页内容",
                "第二页内容",
            ])
            self.assertEqual([chunk["chunk_index"] for chunk in chunks], [0, 1])
            self.assertEqual(stats.documents[0]["node_count"], 2)

    def test_chunk_structure_lazily_renders_table_display_text(self) -> None:
        table_record = {
            "node_type": "table",
            "display_content": None,
            "content": "Sample\tRc\nA\t0.98",
            "metadata": {
                "cells": [
                    ["Sample", "Rc"],
                    ["A", "0.98"],
                ]
            },
        }
        structure = chunk_structure(table_record)
        self.assertIsNotNone(structure["display_content"])
        self.assertIn("| Sample | Rc |", structure["display_content"])
        self.assertIn("| A | 0.98 |", structure["display_content"])

    def test_chunk_structure_keeps_text_display_none(self) -> None:
        text_record = {
            "node_type": "text",
            "display_content": None,
            "content": "普通正文",
            "metadata": {},
        }
        structure = chunk_structure(text_record)
        self.assertIsNone(structure["display_content"])

    def test_incompatible_manifest_requires_rebuild(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            index_dir = Path(temp_dir)
            (index_dir / "manifest.json").write_text(
                json.dumps({"index_format_version": 1}),
                encoding="utf-8",
            )
            (index_dir / "chunks.jsonl").write_text("{}\n", encoding="utf-8")

            with self.assertRaises(IndexFormatError):
                ensure_index_format(index_dir)

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
            node = DocumentNode(
                document_id="doc_new",
                content="新索引内容",
                parser_version="text_parser_v1",
                source_anchor={"source_path": "new.txt"},
                metadata={"filename": "new.txt", "file_type": ".txt"},
            )

            build_index_from_nodes([node], index_dir)

            current = json.loads(
                (index_dir / "manifest.json").read_text(encoding="utf-8")
            )
            archived = list(root.glob("index.old_incompatible_*"))
            self.assertEqual(current["index_format_version"], INDEX_FORMAT_VERSION)
            self.assertEqual(len(archived), 1)
            legacy = json.loads(
                (archived[0] / "manifest.json").read_text(encoding="utf-8")
            )
            self.assertEqual(legacy["index_format_version"], 2)

    def test_old_pdf_parser_version_requires_rebuild(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            index_dir = Path(temp_dir)
            (index_dir / "manifest.json").write_text(
                json.dumps(
                    {
                        "index_format_version": INDEX_FORMAT_VERSION,
                        "parser_versions": ["pdf_parser_v1"],
                    }
                ),
                encoding="utf-8",
            )
            for name in ("nodes.jsonl", "parents.jsonl", "chunks.jsonl"):
                (index_dir / name).write_text("", encoding="utf-8")

            with self.assertRaisesRegex(IndexFormatError, "PDF 解析器版本已升级"):
                ensure_index_format(index_dir)


if __name__ == "__main__":
    unittest.main()
