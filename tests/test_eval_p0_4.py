from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from scripts.eval_p0_4 import compare_reports, evaluate, load_dataset


ROOT_DIR = Path(__file__).resolve().parents[1]
DATASET_PATH = ROOT_DIR / "evals" / "p0_4_domain_extension_cases.json"
BASELINE_PATH = ROOT_DIR / "evals" / "baselines" / "p0_4_domain_rules.json"


class P0_4EvaluationTests(unittest.TestCase):
    def test_committed_dataset_passes_generic_scoring(self) -> None:
        report = evaluate(load_dataset(DATASET_PATH), DATASET_PATH)

        self.assertEqual(report["summary"]["case_count"], 8)
        self.assertEqual(report["summary"]["lite_top1_accuracy"], 1.0)
        self.assertEqual(report["summary"]["reranker_top1_accuracy"], 1.0)

    def test_committed_baseline_matches_dataset_and_current_report(self) -> None:
        baseline = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
        current = evaluate(load_dataset(DATASET_PATH), DATASET_PATH)

        comparison = compare_reports(baseline, current, 0.01)

        self.assertTrue(comparison["passed"])
        self.assertEqual(
            baseline["metadata"]["dataset_sha256"],
            current["metadata"]["dataset_sha256"],
        )

    def test_dataset_validation_rejects_missing_expected_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "invalid.json"
            path.write_text(
                json.dumps(
                    [
                        {
                            "id": "invalid",
                            "query": "test",
                            "expected_candidate": "missing",
                            "candidates": [
                                {"id": "a", "text": "a"},
                                {"id": "b", "text": "b"},
                            ],
                        }
                    ]
                ),
                encoding="utf-8",
            )

            with self.assertRaises(ValueError):
                load_dataset(path)

    def test_comparison_rejects_quality_regression(self) -> None:
        baseline = {
            "metadata": {"dataset_sha256": "same"},
            "summary": {
                "lite_top1_accuracy": 1.0,
                "lite_mrr": 1.0,
                "reranker_top1_accuracy": 1.0,
                "reranker_mrr": 1.0,
            },
        }
        current = {
            "metadata": {"dataset_sha256": "same"},
            "summary": {
                "lite_top1_accuracy": 0.75,
                "lite_mrr": 0.875,
                "reranker_top1_accuracy": 1.0,
                "reranker_mrr": 1.0,
            },
        }

        comparison = compare_reports(baseline, current, 0.01)

        self.assertFalse(comparison["passed"])
        self.assertTrue(comparison["failures"])


if __name__ == "__main__":
    unittest.main()
