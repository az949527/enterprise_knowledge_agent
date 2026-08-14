"""普通内容问答检索无结果时的引导性追问测试。

覆盖 P1 阶段提前实现的"证据不足引导"：检索无结果时不返回生硬提示，
而是给出引导性追问（换说法/指定文件/补充文档），且不调 LLM。
"""
from __future__ import annotations

import asyncio
import unittest
from unittest.mock import patch

from app.lite.generator import answer_query
from app.lite.generator import extractive_answer


class EmptySourcesGuidanceTests(unittest.TestCase):

    def test_empty_sources_returns_guidance_with_empty_mode(self) -> None:
        result = asyncio.run(
            answer_query(
                "模糊的问题",
                [],
                use_llm=True,
                api_key="sk-test12345678",
                base_url="https://example.test/v1",
                model="test-model",
            )
        )
        self.assertEqual(result["mode"], "empty")
        self.assertIn("换一种更明确的说法", result["answer"])
        self.assertIn("添加相关文档", result["answer"])

    def test_empty_sources_does_not_call_llm(self) -> None:
        with patch("openai.AsyncOpenAI") as client_cls:
            asyncio.run(
                answer_query(
                    "模糊的问题",
                    [],
                    use_llm=True,
                    api_key="sk-test12345678",
                    base_url="https://example.test/v1",
                    model="test-model",
                )
            )
        client_cls.assert_not_called()

    def test_extractive_answer_empty_sources_has_guidance(self) -> None:
        text = extractive_answer([])
        self.assertIn("换一种更明确的说法", text)
        self.assertIn("补充相关文档", text)


if __name__ == "__main__":
    unittest.main()
