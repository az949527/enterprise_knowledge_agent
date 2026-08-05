from __future__ import annotations

import unittest

from app.retrieval_signals import (
    looks_like_summary_query,
    noise_penalty,
    query_intent_bonus,
)


class RetrievalSignalTests(unittest.TestCase):
    def test_duration_signal_is_domain_neutral(self) -> None:
        policy_score = query_intent_bonus(
            "账号权限有效期是多久？",
            "临时权限有效期为 30 天，到期后自动失效。",
        )
        production_score = query_intent_bonus(
            "陶瓷坯体需要干燥多久？",
            "坯体需要继续干燥 3 天，之后进入烧制流程。",
        )

        self.assertGreater(policy_score, 0)
        self.assertGreater(production_score, 0)
        self.assertAlmostEqual(policy_score, production_score)

    def test_quantity_and_process_signals_use_generic_evidence(self) -> None:
        amount_score = query_intent_bonus(
            "每次最多可以申请多少台设备？",
            "每次申请不超过 5 台设备。",
        )
        process_score = query_intent_bonus(
            "供应商准入如何办理？",
            "办理流程包括提交申请、合规复核和负责人审批。",
        )

        self.assertGreater(amount_score, 0)
        self.assertGreater(process_score, 0)

    def test_summary_signal_uses_generic_section_markers(self) -> None:
        self.assertTrue(looks_like_summary_query("总结一下这份文档"))
        self.assertGreater(
            query_intent_bonus(
                "总结一下这份文档",
                "概述：本文档说明账号申请、审批和回收流程。",
            ),
            0,
        )

    def test_noise_penalty_targets_low_information_content(self) -> None:
        useful = noise_penalty("报销申请应在费用发生后 30 个自然日内提交。")
        footer = noise_penalty("12\n12\n12\nhttps://example.com")

        self.assertEqual(useful, 0)
        self.assertGreater(footer, useful)


if __name__ == "__main__":
    unittest.main()
