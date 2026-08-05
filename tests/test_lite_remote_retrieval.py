from __future__ import annotations

import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from app.lite.desktop_query import query_desktop_index
from app.lite.indexer import write_chunks
from app.lite.remote_retrieval import (
    EMBEDDING_CACHE_MANIFEST,
    RemoteModelError,
    rerank_sources,
    semantic_search_index,
    set_remote_access,
)
from app.lite.retrieval_cache import (
    build_retrieval_cache_key,
    clear_retrieval_cache,
)


class RemoteRetrievalTests(unittest.TestCase):
    def setUp(self) -> None:
        set_remote_access(True)
        clear_retrieval_cache()
        self.temp_dir = tempfile.TemporaryDirectory()
        self.index_dir = Path(self.temp_dir.name)
        write_chunks(
            self.index_dir,
            [
                {
                    "id": "policy.txt:0",
                    "source_path": "policy.txt",
                    "filename": "policy.txt",
                    "chunk_index": 0,
                    "content": "差旅结束后三十天内提交报销。",
                    "content_chars": 15,
                },
                {
                    "id": "leave.txt:0",
                    "source_path": "leave.txt",
                    "filename": "leave.txt",
                    "chunk_index": 0,
                    "content": "正式员工每年有十天年假。",
                    "content_chars": 13,
                },
            ],
        )

    def tearDown(self) -> None:
        set_remote_access(False)
        clear_retrieval_cache()
        self.temp_dir.cleanup()

    @patch("app.lite.remote_retrieval.embed_texts")
    def test_semantic_search_builds_and_reuses_binary_cache(self, embed_texts_mock) -> None:
        embed_texts_mock.side_effect = [
            [[1.0, 0.0], [0.0, 1.0]],
            [[0.9, 0.1]],
        ]
        first = semantic_search_index(
            "出差如何报销",
            self.index_dir,
            top_k=2,
            api_key="test-key",
            base_url="https://example.test/v1",
            model="embedding-test",
        )
        self.assertEqual(first[0]["filename"], "policy.txt")
        self.assertEqual(embed_texts_mock.call_count, 2)

        manifest = json.loads(
            (self.index_dir / EMBEDDING_CACHE_MANIFEST).read_text(encoding="utf-8")
        )
        self.assertEqual(manifest["dimension"], 2)
        self.assertNotIn("api_key", manifest)
        self.assertNotIn("test-key", json.dumps(manifest))

        embed_texts_mock.reset_mock()
        embed_texts_mock.side_effect = None
        embed_texts_mock.return_value = [[0.1, 0.9]]
        second = semantic_search_index(
            "员工有多少年假",
            self.index_dir,
            top_k=2,
            api_key="test-key",
            base_url="https://example.test/v1",
            model="embedding-test",
        )
        self.assertEqual(second[0]["filename"], "leave.txt")
        self.assertEqual(embed_texts_mock.call_count, 1)

    @patch("app.lite.remote_retrieval.embed_texts")
    def test_embedding_cache_reuses_unchanged_chunk_vectors(
        self,
        embed_texts_mock,
    ) -> None:
        embed_texts_mock.side_effect = [
            [[1.0, 0.0], [0.0, 1.0]],
            [[1.0, 0.0]],
        ]
        semantic_search_index(
            "报销",
            self.index_dir,
            top_k=2,
            api_key="test-key",
            base_url="https://example.test/v1",
            model="embedding-test",
        )

        write_chunks(
            self.index_dir,
            [
                {
                    "id": "policy.txt:0",
                    "source_path": "policy.txt",
                    "filename": "policy.txt",
                    "chunk_index": 0,
                    "content": "差旅结束后三十天内提交报销。",
                },
                {
                    "id": "leave.txt:0",
                    "source_path": "leave.txt",
                    "filename": "leave.txt",
                    "chunk_index": 0,
                    "content": "正式员工每年有十五天年假。",
                },
                {
                    "id": "security.txt:0",
                    "source_path": "security.txt",
                    "filename": "security.txt",
                    "chunk_index": 0,
                    "content": "安全事件应在一小时内报告。",
                },
            ],
        )
        embed_texts_mock.reset_mock()
        embed_texts_mock.side_effect = [
            [[0.0, 1.0], [0.5, 0.5]],
            [[0.0, 1.0]],
        ]

        semantic_search_index(
            "年假",
            self.index_dir,
            top_k=3,
            api_key="test-key",
            base_url="https://example.test/v1",
            model="embedding-test",
        )

        self.assertEqual(embed_texts_mock.call_count, 2)
        document_texts = embed_texts_mock.call_args_list[0].args[0]
        self.assertEqual(len(document_texts), 2)
        self.assertIn("文件名：leave.txt", document_texts[0])
        self.assertIn("正式员工每年有十五天年假。", document_texts[0])
        self.assertIn("文件名：security.txt", document_texts[1])
        self.assertIn("安全事件应在一小时内报告。", document_texts[1])

    def test_retrieval_cache_key_includes_top_k_and_index_fingerprint(self) -> None:
        common = {
            "query": "年假",
            "index_dir": self.index_dir,
            "mode": "bm25",
            "models": {},
            "parameters": {"candidate_k": 5},
        }
        first = build_retrieval_cache_key(top_k=5, **common)
        different_top_k = build_retrieval_cache_key(top_k=10, **common)
        self.assertNotEqual(first, different_top_k)

        write_chunks(
            self.index_dir,
            [
                {
                    "id": "changed.txt:0",
                    "content": "changed index content",
                }
            ],
        )
        changed_index = build_retrieval_cache_key(top_k=5, **common)
        self.assertNotEqual(first, changed_index)

    @patch("app.lite.desktop_query.search_bm25_index")
    def test_desktop_query_reuses_retrieval_cache(
        self,
        search_mock,
    ) -> None:
        search_mock.return_value = [
            {
                "filename": "policy.txt",
                "source_path": "policy.txt",
                "chunk_index": 0,
                "content": "差旅结束后三十天内提交报销。",
                "rank": 1,
                "score": 1.0,
            }
        ]
        arguments = {
            "query": "如何报销",
            "index_dir": self.index_dir,
            "use_llm": False,
            "llm_api_key": "",
            "llm_base_url": "",
            "llm_model": "",
            "use_embedding": False,
            "use_reranker": False,
            "retrieval_api_key": "",
        }
        first = asyncio.run(query_desktop_index(**arguments))
        second = asyncio.run(query_desktop_index(**arguments))

        self.assertEqual(search_mock.call_count, 1)
        self.assertFalse(first["retrieval"]["cache_hit"])
        self.assertTrue(second["retrieval"]["cache_hit"])

    @patch("app.lite.remote_retrieval._post_json")
    def test_reranker_maps_remote_order_and_score(self, post_json_mock) -> None:
        post_json_mock.return_value = {
            "results": [
                {"index": 1, "relevance_score": 0.98},
                {"index": 0, "relevance_score": 0.12},
            ]
        }
        candidates = [
            {"filename": "policy.txt", "content": "报销制度", "rank": 1, "score": 0.7},
            {"filename": "leave.txt", "content": "年假制度", "rank": 2, "score": 0.6},
        ]
        ranked = rerank_sources(
            "一年有多少年假",
            candidates,
            top_n=2,
            api_key="test-key",
            base_url="https://example.test/v1",
            model="reranker-test",
        )
        self.assertEqual([item["filename"] for item in ranked], ["leave.txt", "policy.txt"])
        self.assertEqual(ranked[0]["rank"], 1)
        self.assertEqual(ranked[0]["rerank_score"], 0.98)

    def test_missing_embedding_key_returns_error_without_sources(self) -> None:
        result = asyncio.run(
            query_desktop_index(
                "出差如何报销",
                self.index_dir,
                use_llm=False,
                llm_api_key="",
                llm_base_url="",
                llm_model="",
                use_embedding=True,
                use_reranker=False,
                retrieval_api_key="",
            )
        )
        self.assertEqual(result["mode"], "embedding_error")
        self.assertEqual(result["sources"], [])
        self.assertEqual(result["retrieved_sources"], [])

    @patch("app.lite.desktop_query.rerank_sources")
    @patch("app.lite.desktop_query.search_bm25_index")
    def test_reranker_failure_returns_error_without_sources(
        self,
        search_bm25_index_mock,
        rerank_sources_mock,
    ) -> None:
        search_bm25_index_mock.return_value = [
            {"filename": "policy.txt", "content": "报销制度", "rank": 1, "score": 0.7}
        ]
        rerank_sources_mock.side_effect = RemoteModelError(
            "reranker_error",
            "Reranker 请求失败（HTTP 401）：invalid key",
        )
        result = asyncio.run(
            query_desktop_index(
                "出差如何报销",
                self.index_dir,
                use_llm=False,
                llm_api_key="",
                llm_base_url="",
                llm_model="",
                use_embedding=False,
                use_reranker=True,
                retrieval_api_key="bad-key",
            )
        )
        self.assertEqual(result["mode"], "reranker_error")
        self.assertEqual(result["sources"], [])
        self.assertEqual(result["retrieved_sources"], [])


if __name__ == "__main__":
    unittest.main()
