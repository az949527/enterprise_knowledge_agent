from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from app.lite.bm25_search import BM25_INDEX_FILE, search_bm25_index
from app.lite.indexer import write_chunks


class Bm25SearchTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.index_dir = Path(self.temp_dir.name)
        self._write_default_chunks()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_chinese_query_returns_relevant_chunk_first(self) -> None:
        results = search_bm25_index(
            "出差产生的费用应该怎样报销？",
            self.index_dir,
            top_k=3,
        )
        self.assertEqual(results[0]["filename"], "travel.txt")
        self.assertTrue((self.index_dir / BM25_INDEX_FILE).exists())

    def test_exact_technical_values_are_searchable(self) -> None:
        results = search_bm25_index(
            "哪个模型的 RMSEP 是 0.6431？",
            self.index_dir,
            top_k=3,
        )
        self.assertEqual(results[0]["filename"], "model-results.txt")

    def test_cache_rebuilds_when_chunks_change(self) -> None:
        search_bm25_index("报销", self.index_dir, top_k=2)
        before = self._metadata()

        chunks = self._chunks()
        chunks.append(
            {
                "id": "security.txt:0",
                "source_path": "security.txt",
                "filename": "security.txt",
                "chunk_index": 0,
                "content": "办公系统密码每九十天必须更换一次。",
                "content_chars": 18,
            }
        )
        write_chunks(self.index_dir, chunks)
        results = search_bm25_index("密码多久更换", self.index_dir, top_k=2)
        after = self._metadata()

        self.assertEqual(results[0]["filename"], "security.txt")
        self.assertNotEqual(before["fingerprint"], after["fingerprint"])
        self.assertEqual(after["chunk_count"], "4")

    def test_corrupt_cache_is_diagnosed_by_rebuilding(self) -> None:
        search_bm25_index("报销", self.index_dir, top_k=2)
        (self.index_dir / BM25_INDEX_FILE).write_bytes(b"not a sqlite database")

        results = search_bm25_index("密码多久更换", self.index_dir, top_k=2)

        self.assertTrue(results)
        self.assertEqual(self._metadata()["chunk_count"], "3")

    def _write_default_chunks(self) -> None:
        write_chunks(
            self.index_dir,
            [
                {
                    "id": "travel.txt:0",
                    "source_path": "travel.txt",
                    "filename": "travel.txt",
                    "chunk_index": 0,
                    "content": "员工应在差旅结束后的三十个自然日内提交费用报销申请。",
                    "content_chars": 28,
                },
                {
                    "id": "leave.txt:0",
                    "source_path": "leave.txt",
                    "filename": "leave.txt",
                    "chunk_index": 0,
                    "content": "正式员工每年享有十个工作日的带薪年假。",
                    "content_chars": 22,
                },
                {
                    "id": "model-results.txt:0",
                    "source_path": "model-results.txt",
                    "filename": "model-results.txt",
                    "chunk_index": 0,
                    "content": "咸蛋黄指数模型的 Rp 为 0.9163，RMSEP 为 0.6431。",
                    "content_chars": 34,
                },
            ],
        )

    def _chunks(self) -> list[dict]:
        return [
            json.loads(line)
            for line in (self.index_dir / "chunks.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def _metadata(self) -> dict[str, str]:
        connection = sqlite3.connect(self.index_dir / BM25_INDEX_FILE)
        try:
            return dict(connection.execute("SELECT key, value FROM metadata"))
        finally:
            connection.close()


if __name__ == "__main__":
    unittest.main()
