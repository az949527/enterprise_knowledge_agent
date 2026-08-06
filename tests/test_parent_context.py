from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from app.documents import DocumentNode, NodeType
from app.lite.bm25_search import search_bm25_index
from app.lite.generator import build_context
from app.lite.indexer import build_index, read_chunks, write_node_index
from app.lite.parent_context import ParentContextResolver, _truncate


def _source_for_chunk(
    chunk: dict,
    *,
    content: str | None = None,
) -> dict:
    """构造一条与 bm25_search._record_to_result 同形状的检索命中记录。"""
    return {
        "document_id": str(chunk.get("document_id") or ""),
        "node_id": str(chunk.get("node_id") or ""),
        "parent_id": str(chunk.get("parent_id") or ""),
        "filename": str(chunk.get("filename") or ""),
        "chunk_index": int(chunk.get("chunk_index") or 0),
        "content": content if content is not None else str(chunk.get("content") or ""),
    }


_KEEP_ALIVE: list[tempfile.TemporaryDirectory] = []


def _monolithic_index(nodes: list[DocumentNode]) -> Path:
    directory = tempfile.TemporaryDirectory()
    _KEEP_ALIVE.append(directory)  # 保持引用，避免目录被提前回收
    index_dir = Path(directory.name)
    write_node_index(
        nodes,
        source_label="test",
        index_dir=index_dir,
        chunk_size=40,
        chunk_overlap=10,
    )
    return index_dir


class ParentContextResolverTests(unittest.TestCase):
    def test_text_parent_content_attached(self) -> None:
        node = DocumentNode(
            document_id="doc_policy",
            content=("差旅结束后三十天内提交报销。" * 20),
            parser_version="test_parser_v1",
            node_type=NodeType.TEXT,
            metadata={"filename": "policy.txt", "file_type": ".txt"},
        )
        index_dir = _monolithic_index([node])
        chunks = read_chunks(index_dir)
        self.assertGreater(len(chunks), 1)  # 一个节点被切成多个小块
        source = _source_for_chunk(chunks[0])

        ParentContextResolver(index_dir).resolve([source])

        self.assertEqual(source["parent_content"], node.content)
        self.assertEqual(source["parent_node_id"], node.node_id)
        self.assertEqual(source["content"], chunks[0]["content"])  # child 不变

    def test_table_parent_uses_display_content(self) -> None:
        node = DocumentNode(
            document_id="doc_budget",
            content="部门\t预算\n研发部\t120000",
            parser_version="test_parser_v1",
            node_type=NodeType.TABLE,
            display_content="| 部门 | 预算 |\n| --- | --- |\n| 研发部 | 120000 |",
            metadata={"filename": "budget.csv", "file_type": ".csv"},
        )
        index_dir = _monolithic_index([node])
        chunks = read_chunks(index_dir)
        source = _source_for_chunk(chunks[0])

        ParentContextResolver(index_dir).resolve([source])

        # 表格父节点用渲染后的 Markdown 作为父上下文。
        self.assertEqual(source["parent_content"], node.display_content)
        self.assertNotIn("parent_display_content", source)  # 与父内容相同则不冗余

    def test_dedupe_when_child_content_equals_parent(self) -> None:
        node = DocumentNode(
            document_id="doc_short",
            content="短文本",
            parser_version="test_parser_v1",
            node_type=NodeType.TEXT,
            metadata={"filename": "short.txt", "file_type": ".txt"},
        )
        index_dir = _monolithic_index([node])
        chunks = read_chunks(index_dir)
        self.assertEqual(len(chunks), 1)
        source = _source_for_chunk(chunks[0])

        ParentContextResolver(index_dir).resolve([source])

        self.assertNotIn("parent_content", source)

    def test_per_parent_truncation(self) -> None:
        node = DocumentNode(
            document_id="doc_long",
            content="段落内容。" * 100,
            parser_version="test_parser_v1",
            node_type=NodeType.TEXT,
            metadata={"filename": "long.txt", "file_type": ".txt"},
        )
        index_dir = _monolithic_index([node])
        chunks = read_chunks(index_dir)
        source = _source_for_chunk(chunks[0])

        ParentContextResolver(index_dir, max_parent_chars=30).resolve([source])

        self.assertLessEqual(len(source.get("parent_content", "")), 30)

    def test_total_cap_stops_later_sources(self) -> None:
        node = DocumentNode(
            document_id="doc_cap",
            content="内容。" * 80,
            parser_version="test_parser_v1",
            node_type=NodeType.TEXT,
            metadata={"filename": "cap.txt", "file_type": ".txt"},
        )
        index_dir = _monolithic_index([node])
        chunks = read_chunks(index_dir)
        sources = [
            _source_for_chunk(chunks[0]),
            _source_for_chunk(chunks[1]),
            _source_for_chunk(chunks[2]),
        ]

        ParentContextResolver(index_dir, max_total_chars=10).resolve(sources)

        attached = [source for source in sources if "parent_content" in source]
        self.assertLessEqual(len(attached), 1)

    def test_cross_shard_resolution(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source_dir = root / "documents"
            index_dir = root / "index"
            source_dir.mkdir()
            (source_dir / "a.txt").write_text("A制度内容。" * 200, encoding="utf-8")
            (source_dir / "b.txt").write_text("B制度内容。" * 200, encoding="utf-8")
            build_index(source_dir, index_dir, chunk_size=50, chunk_overlap=10)

            chunks = read_chunks(index_dir)
            sources = []
            seen_docs: set[str] = set()
            for chunk in chunks:  # 每份文档各取一个 chunk
                doc_id = str(chunk.get("document_id") or "")
                if doc_id in seen_docs:
                    continue
                seen_docs.add(doc_id)
                sources.append(_source_for_chunk(chunk))
                if len(sources) == 2:
                    break

            ParentContextResolver(index_dir).resolve(sources)

            self.assertEqual(len(sources), 2)
            for source in sources:
                self.assertIn("parent_content", source)

    def test_missing_parent_graceful(self) -> None:
        node = DocumentNode(
            document_id="doc_missing",
            content="内容。" * 20,
            parser_version="test_parser_v1",
            node_type=NodeType.TEXT,
            metadata={"filename": "m.txt", "file_type": ".txt"},
        )
        index_dir = _monolithic_index([node])

        no_parent = {
            "document_id": "doc_missing",
            "node_id": "unknown",
            "parent_id": "",
            "filename": "m.txt",
            "chunk_index": 0,
            "content": "内容。",
        }
        unknown_doc = dict(no_parent)
        unknown_doc["parent_id"] = "node_does_not_exist"
        unknown_doc["document_id"] = "doc_not_in_index"

        resolver = ParentContextResolver(index_dir)
        resolver.resolve([no_parent, unknown_doc])

        self.assertNotIn("parent_content", no_parent)
        self.assertNotIn("parent_content", unknown_doc)

    def test_legacy_monolithic_index_resolves(self) -> None:
        # write_node_index 产出的顶层单体布局即“旧单体”，分片路径不存在时应回退顶层。
        node = DocumentNode(
            document_id="doc_legacy",
            content="旧制度内容。" * 20,
            parser_version="test_parser_v1",
            node_type=NodeType.TEXT,
            metadata={"filename": "legacy.txt", "file_type": ".txt"},
        )
        index_dir = _monolithic_index([node])
        chunks = read_chunks(index_dir)
        source = _source_for_chunk(chunks[0])

        ParentContextResolver(index_dir).resolve([source])

        self.assertEqual(source["parent_content"], node.content)

    def test_truncate_cjk_boundary(self) -> None:
        self.assertEqual(len(_truncate("中文内容" * 100, 10)), 10)
        self.assertEqual(_truncate("abc", 10), "abc")
        self.assertEqual(_truncate("abc", 0), "")


class ParentContextIntegrationTests(unittest.TestCase):
    def test_query_enriches_sources_and_build_context(self) -> None:
        node = DocumentNode(
            document_id="doc_int",
            content="报销需在差旅结束后三十天内提交。延迟需说明原因。" * 15,
            parser_version="test_parser_v1",
            node_type=NodeType.TEXT,
            metadata={"filename": "reimburse.txt", "file_type": ".txt"},
        )
        index_dir = _monolithic_index([node])
        results = search_bm25_index("报销提交时限", index_dir, top_k=1)

        self.assertEqual(len(results), 1)
        ParentContextResolver(index_dir).resolve(results)
        self.assertIn("parent_content", results[0])

        context = build_context(results)
        self.assertIn("所属小节上下文", context)
        self.assertIn(">> 命中的具体片段 <<", context)
        self.assertIn(results[0]["content"], context)
        self.assertIn(node.content, context)


if __name__ == "__main__":
    unittest.main()
