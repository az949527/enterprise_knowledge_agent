from __future__ import annotations

import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest


ROOT_DIR = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = ROOT_DIR / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from eval_p0_1 import (
    DatasetValidationError,
    _citation_accuracy,
    _score_case,
    calculate_api_cost,
    compare_reports,
    current_rss_bytes,
    load_manifest,
    validate_frozen_inputs,
)


class P0_1EvaluationTests(unittest.TestCase):
    def test_frozen_manifest_matches_current_inputs(self):
        manifest = load_manifest()
        result = validate_frozen_inputs(
            manifest,
            ROOT_DIR / manifest["dataset"],
            ROOT_DIR / manifest["documents_dir"],
        )
        self.assertEqual(result["dataset_sha256"], manifest["dataset_sha256"])
        self.assertEqual(
            result["documents_sha256"], manifest["documents_sha256"]
        )

    def test_committed_baseline_contains_all_required_metrics(self):
        manifest = load_manifest()
        report = json.loads(
            (ROOT_DIR / manifest["baseline_report"]).read_text(encoding="utf-8")
        )
        missing = set(manifest["required_metrics"]) - set(report["summary"])
        self.assertFalse(missing)
        self.assertEqual(
            report["metadata"]["dataset_sha256"],
            manifest["dataset_sha256"],
        )

    def test_frozen_manifest_rejects_changed_dataset(self):
        manifest = load_manifest()
        original = json.loads(
            (ROOT_DIR / manifest["dataset"]).read_text(encoding="utf-8")
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            changed = Path(temp_dir) / "changed.json"
            changed.write_text(
                json.dumps(original[:-1], ensure_ascii=False),
                encoding="utf-8",
            )
            with self.assertRaises(DatasetValidationError):
                validate_frozen_inputs(
                    manifest,
                    changed,
                    ROOT_DIR / manifest["documents_dir"],
                )

    def test_score_case_uses_answer_text_for_coverage(self):
        item = {
            "id": "case-1",
            "question": "question",
            "expected_document_contains": "policy.md",
            "expected_chunk_indices": [0],
            "expected_answer_terms": ["one day"],
        }
        sources = [
            {
                "rank": 1,
                "filename": "policy.md",
                "chunk_index": 0,
                "content": "Submit one day early.",
                "score": 1.0,
            }
        ]
        result = _score_case(
            item,
            sources,
            {
                "answer": "The answer omitted the required duration [1].",
                "mode": "local_fallback",
            },
            12.5,
        )
        self.assertTrue(result["retrieval_hit"])
        self.assertEqual(result["answer_coverage"], 0.0)
        self.assertEqual(result["citation_accuracy"], 1.0)

    def test_citation_accuracy_is_precision(self):
        sources = [
            {"filename": "policy.md", "chunk_index": 0},
            {"filename": "unrelated.md", "chunk_index": 0},
        ]
        score = _citation_accuracy([1, 2], sources, "policy.md", {0})
        self.assertEqual(score, 0.5)

    def test_refusal_requires_refusal_language_without_citations(self):
        item = {
            "id": "refusal-1",
            "type": "refusal",
            "question": "unknown",
            "expected_refusal": True,
            "expected_answer_terms": [["资料不足", "无法回答"]],
        }
        result = _score_case(
            item,
            [],
            {"answer": "当前资料不足以回答。", "mode": "empty"},
            1.0,
        )
        self.assertTrue(result["refusal_correct"])
        self.assertEqual(result["citation_accuracy"], 1.0)
        self.assertFalse(result["retrieval_expected"])

    def test_quality_gate_rejects_material_regression(self):
        before = make_report()
        after = copy.deepcopy(before)
        after["summary"]["recall_at_5"] = 0.95
        after["summary"]["type_metrics"]["plain_text"]["recall_at_5"] = 0.95
        comparison = compare_reports(before, after)
        self.assertFalse(comparison["passed"])
        self.assertTrue(comparison["failures"])

    def test_quality_gate_rejects_incomparable_dataset(self):
        before = make_report()
        after = copy.deepcopy(before)
        after["metadata"]["dataset_sha256"] = "different"
        comparison = compare_reports(before, after)
        self.assertFalse(comparison["passed"])
        self.assertTrue(
            any(
                "dataset_sha256 mismatch" in item
                for item in comparison["failures"]
            )
        )

    def test_api_cost_uses_input_and_output_rates(self):
        cost = calculate_api_cost(
            {
                "prompt_tokens": 1_000_000,
                "completion_tokens": 500_000,
                "total_tokens": 1_500_000,
            },
            use_llm=True,
            input_cost_per_million=2.0,
            output_cost_per_million=6.0,
        )
        self.assertEqual(cost, 5.0)

    def test_process_memory_measurement_is_available(self):
        rss_bytes, method = current_rss_bytes()
        self.assertGreater(rss_bytes, 0, method)


def make_report():
    quality = {
        "recall_at_5": 1.0,
        "mrr": 1.0,
        "answer_coverage": 1.0,
        "citation_accuracy": 1.0,
    }
    summary = {
        **quality,
        "case_count": 30,
        "top_k": 5,
        "avg_latency_ms": 10.0,
        "p95_latency_ms": 20.0,
        "index_elapsed_ms": 30.0,
        "index_peak_memory_bytes": 100.0,
        "index_disk_bytes": 200.0,
        "api_tokens": 0,
        "api_cost_usd": 0.0,
        "type_metrics": {
            "plain_text": {"case_count": 30, **quality}
        },
    }
    return {
        "metadata": {
            "baseline_id": "baseline",
            "dataset_sha256": "dataset",
            "case_ids_sha256": "cases",
        },
        "summary": summary,
    }


if __name__ == "__main__":
    unittest.main()
