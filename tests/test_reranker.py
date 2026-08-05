from __future__ import annotations

import unittest

from app.rag.reranker import Reranker, _local_rerank_score


class LocalRerankerTests(unittest.TestCase):
    def test_time_evidence_beats_domain_vocabulary_without_an_answer(self) -> None:
        query = "烘干流程需要多久？"
        evidence = "烘干流程持续 8 小时，完成后进入质检步骤。"
        domain_terms_only = "烘干流程包含温控模型、光谱检测和预处理参数。"

        self.assertGreater(
            _local_rerank_score(query, evidence, 0.5),
            _local_rerank_score(query, domain_terms_only, 0.5),
        )

    def test_rule_reranker_handles_enterprise_process_without_model(self) -> None:
        reranker = Reranker(use_model=False)
        candidates = [
            ("供应商目录包含硬件、软件和咨询服务分类。", 0.6),
            ("准入流程包括提交申请、合规复核和负责人审批。", 0.6),
        ]

        results = reranker.rerank("供应商准入如何办理？", candidates, top_n=2)

        self.assertEqual(results[0][0][0], candidates[1][0])


if __name__ == "__main__":
    unittest.main()
