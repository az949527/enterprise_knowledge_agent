from __future__ import annotations

import asyncio
from io import BytesIO
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from openpyxl import Workbook

from app.documents import (
    DocumentNode,
    NodeType,
    document_id_from_source,
    iter_document_nodes_from_bytes,
)
from app.lite.bm25_search import search_bm25_index
from app.lite.desktop_query import query_desktop_index
from app.lite.indexer import write_node_index


class ExcelQueryPlanningTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.index_dir = Path(self.temp_dir.name) / "index"
        nodes = [
            *iter_document_nodes_from_bytes(
                "20230526.xlsx",
                _workbook_bytes(
                    ["指标", "工业利润", "核心CPI"],
                    [["2026-01", 12.5, 0.8]],
                ),
            ),
            *iter_document_nodes_from_bytes(
                "final_df.xlsx",
                _workbook_bytes(
                    ["时间", "中证500", "沪深300"],
                    [["2026-01", 5300, 4100]],
                ),
            ),
            DocumentNode(
                document_id=document_id_from_source("noise.pdf"),
                content="报告中提到两个模型和若干表格，但不属于 Excel 文件清单。",
                parser_version="pdf_parser_v3",
                node_type=NodeType.TEXT,
                source_anchor={"source_path": "noise.pdf"},
                metadata={"filename": "noise.pdf", "file_type": ".pdf"},
            ),
        ]
        write_node_index(
            nodes,
            source_label="test",
            index_dir=self.index_dir,
            chunk_size=900,
            chunk_overlap=120,
        )

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_excel_inventory_count_uses_manifest_without_remote_calls(self) -> None:
        with (
            patch(
                "app.lite.desktop_query.semantic_search_index"
            ) as semantic_mock,
            patch("app.lite.desktop_query.rerank_sources") as rerank_mock,
        ):
            result = asyncio.run(
                self._query(
                    "我有几个 Excel？",
                    offline=False,
                    use_llm=True,
                    use_embedding=True,
                    use_reranker=True,
                )
            )

        self.assertEqual(result["mode"], "structured")
        self.assertIn("2 个 Excel 文件", result["answer"])
        self.assertIn("20230526.xlsx", result["answer"])
        self.assertIn("final_df.xlsx", result["answer"])
        self.assertNotIn("noise.pdf", result["answer"])
        self.assertFalse(result["retrieval"]["remote"])
        semantic_mock.assert_not_called()
        rerank_mock.assert_not_called()

    def test_multi_excel_summary_returns_each_workbook_once(self) -> None:
        result = asyncio.run(self._query("我的 Excel 表有什么？"))

        self.assertEqual(
            [source["filename"] for source in result["retrieved_sources"]],
            ["20230526.xlsx", "final_df.xlsx"],
        )
        self.assertTrue(
            all(
                source["node_type"] == "workbook_summary"
                for source in result["retrieved_sources"]
            )
        )
        self.assertIn("20230526.xlsx", result["answer"])
        self.assertIn("final_df.xlsx", result["answer"])
        self.assertNotIn("noise.pdf", result["answer"])

    def test_generic_table_summary_covers_all_excel_workbooks(self) -> None:
        result = asyncio.run(self._query("表里有什么内容？"))

        self.assertEqual(
            {source["filename"] for source in result["retrieved_sources"]},
            {"20230526.xlsx", "final_df.xlsx"},
        )

    def test_excel_content_summary_covers_each_workbook(self) -> None:
        """用户报告回归：上传两个 Excel 问"excel里面什么内容"只回答一个。"""
        result = asyncio.run(self._query("excel里面什么内容"))

        self.assertEqual(
            {source["filename"] for source in result["retrieved_sources"]},
            {"20230526.xlsx", "final_df.xlsx"},
        )
        self.assertIn("20230526.xlsx", result["answer"])
        self.assertIn("final_df.xlsx", result["answer"])

    def test_excel_main_content_summary_covers_each_workbook(self) -> None:
        """"excel里面主要讲了什么"应同样走多文档概览，覆盖每个 Excel。"""
        result = asyncio.run(self._query("excel里面主要讲了什么"))

        self.assertEqual(
            {source["filename"] for source in result["retrieved_sources"]},
            {"20230526.xlsx", "final_df.xlsx"},
        )
        self.assertIn("20230526.xlsx", result["answer"])
        self.assertIn("final_df.xlsx", result["answer"])

    def test_summary_falls_back_to_structured_when_llm_empty(self) -> None:
        """用户报告回归：概览查询 LLM 返回空/失败时，回退结构化摘要，
        不应出现"没有回答"只有引用来源。"""
        with patch(
            "app.lite.desktop_query.answer_query",
            new=AsyncMock(
                return_value={
                    "answer": "",
                    "mode": "llm_error",
                    "llm": {"enabled": True, "usage": None},
                }
            ),
        ):
            result = asyncio.run(
                self._query("excel里面什么内容", use_llm=True)
            )

        self.assertEqual(result["mode"], "local_fallback")
        self.assertIn("20230526.xlsx", result["answer"])
        self.assertIn("final_df.xlsx", result["answer"])

    def test_filename_summary_scopes_to_matching_workbook(self) -> None:
        result = asyncio.run(self._query("20230526.xlsx 有什么？"))

        self.assertEqual(
            [source["filename"] for source in result["retrieved_sources"]],
            ["20230526.xlsx"],
        )
        self.assertIn("工业利润", result["answer"])
        self.assertNotIn("中证500", result["answer"])

    def test_filename_metadata_is_searchable_and_scopes_content_query(self) -> None:
        lexical = search_bm25_index(
            "final_df.xlsx 中证500",
            self.index_dir,
            top_k=3,
        )
        scoped = asyncio.run(
            self._query("final_df.xlsx 的中证500是多少？")
        )

        self.assertEqual(lexical[0]["filename"], "final_df.xlsx")
        self.assertTrue(
            all(
                source["filename"] == "final_df.xlsx"
                for source in scoped["retrieved_sources"]
            )
        )
        self.assertIn("5300", scoped["answer"])

    async def _query(
        self,
        query: str,
        *,
        offline: bool = True,
        use_llm: bool = False,
        use_embedding: bool = False,
        use_reranker: bool = False,
    ) -> dict:
        return await query_desktop_index(
            query,
            self.index_dir,
            use_llm=use_llm,
            llm_api_key="test-key",
            llm_base_url="https://example.test/v1",
            llm_model="test-model",
            use_embedding=use_embedding,
            use_reranker=use_reranker,
            retrieval_api_key="test-key",
            offline=offline,
        )


def _workbook_bytes(
    headers: list[str],
    rows: list[list[object]],
) -> bytes:
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Sheet1"
    sheet.append(headers)
    for row in rows:
        sheet.append(row)
    stream = BytesIO()
    workbook.save(stream)
    workbook.close()
    return stream.getvalue()


if __name__ == "__main__":
    unittest.main()
