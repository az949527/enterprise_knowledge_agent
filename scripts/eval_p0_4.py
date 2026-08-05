from __future__ import annotations

import argparse
from datetime import datetime
import hashlib
import json
from pathlib import Path
import statistics
import sys
from time import perf_counter


ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.lite.search import lexical_score, lexical_terms
from app.rag.reranker import _local_rerank_score
from app.retrieval_signals import noise_penalty, query_intent_bonus


DEFAULT_DATASET = ROOT_DIR / "evals" / "p0_4_domain_extension_cases.json"
DEFAULT_BASELINE = ROOT_DIR / "evals" / "baselines" / "p0_4_domain_rules.json"
DEFAULT_OUTPUT = ROOT_DIR / "outputs" / "evals"
QUALITY_METRICS = (
    "lite_top1_accuracy",
    "lite_mrr",
    "reranker_top1_accuracy",
    "reranker_mrr",
)


def load_dataset(path: str | Path) -> list[dict]:
    dataset = json.loads(Path(path).resolve().read_text(encoding="utf-8"))
    if not isinstance(dataset, list) or not dataset:
        raise ValueError("P0-4 domain extension dataset must be a non-empty array.")
    for case in dataset:
        candidate_ids = {
            str(candidate.get("id") or "")
            for candidate in case.get("candidates") or []
        }
        if (
            not case.get("id")
            or not case.get("query")
            or len(candidate_ids) < 2
            or str(case.get("expected_candidate") or "") not in candidate_ids
        ):
            raise ValueError(f"Invalid P0-4 case: {case.get('id')}")
    return dataset


def evaluate(dataset: list[dict], dataset_path: str | Path) -> dict:
    cases = []
    lite_latencies = []
    reranker_latencies = []
    for case in dataset:
        query = str(case["query"])
        candidates = list(case["candidates"])

        started = perf_counter()
        lite_ranking = sorted(
            candidates,
            key=lambda candidate: _lite_score(query, candidate["text"]),
            reverse=True,
        )
        lite_latencies.append((perf_counter() - started) * 1_000_000)

        started = perf_counter()
        reranker_ranking = sorted(
            candidates,
            key=lambda candidate: _local_rerank_score(
                query,
                candidate["text"],
                float(candidate.get("faiss_score") or 0.0),
            ),
            reverse=True,
        )
        reranker_latencies.append((perf_counter() - started) * 1_000_000)

        expected = str(case["expected_candidate"])
        lite_rank = _rank_of(lite_ranking, expected)
        reranker_rank = _rank_of(reranker_ranking, expected)
        cases.append(
            {
                "id": case["id"],
                "domain": case.get("domain"),
                "expected_candidate": expected,
                "lite_rank": lite_rank,
                "reranker_rank": reranker_rank,
                "lite_order": [candidate["id"] for candidate in lite_ranking],
                "reranker_order": [candidate["id"] for candidate in reranker_ranking],
            }
        )

    summary = {
        "case_count": len(cases),
        "lite_top1_accuracy": _mean(item["lite_rank"] == 1 for item in cases),
        "lite_mrr": _mean(1.0 / item["lite_rank"] for item in cases),
        "reranker_top1_accuracy": _mean(
            item["reranker_rank"] == 1 for item in cases
        ),
        "reranker_mrr": _mean(1.0 / item["reranker_rank"] for item in cases),
        "lite_avg_latency_us": statistics.fmean(lite_latencies),
        "reranker_avg_latency_us": statistics.fmean(reranker_latencies),
    }
    return {
        "metadata": {
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "dataset": _relative_to_root(dataset_path),
            "dataset_sha256": hashlib.sha256(
                Path(dataset_path).resolve().read_bytes()
            ).hexdigest(),
            "strategy": "generic_retrieval_signals",
        },
        "summary": summary,
        "cases": cases,
    }


def compare_reports(before: dict, after: dict, max_quality_drop: float) -> dict:
    failures = []
    if (
        before.get("metadata", {}).get("dataset_sha256")
        != after.get("metadata", {}).get("dataset_sha256")
    ):
        failures.append("dataset SHA-256 mismatch")

    metrics = {}
    for metric in QUALITY_METRICS:
        old_value = float(before["summary"][metric])
        new_value = float(after["summary"][metric])
        delta = new_value - old_value
        metrics[metric] = {
            "before": old_value,
            "after": new_value,
            "delta": delta,
        }
        if delta < -max_quality_drop:
            failures.append(
                f"{metric} dropped by {abs(delta):.4f}; limit is {max_quality_drop:.4f}"
            )
    return {
        "passed": not failures,
        "max_quality_drop": max_quality_drop,
        "metrics": metrics,
        "failures": failures,
    }


def save_report(report: dict, output_dir: str | Path) -> tuple[Path, Path]:
    output_path = Path(output_dir).resolve()
    output_path.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    json_path = output_path / f"p0_4_eval_{stamp}.json"
    markdown_path = output_path / f"p0_4_eval_{stamp}.md"
    json_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    markdown_path.write_text(_render_markdown(report), encoding="utf-8")
    return json_path, markdown_path


def _lite_score(query: str, text: str) -> float:
    return (
        lexical_score(lexical_terms(query), text)
        + query_intent_bonus(query, text)
        - noise_penalty(text)
    )


def _rank_of(ranking: list[dict], expected: str) -> int:
    for rank, candidate in enumerate(ranking, 1):
        if str(candidate.get("id") or "") == expected:
            return rank
    raise ValueError(f"Expected candidate not ranked: {expected}")


def _mean(values) -> float:
    items = [float(value) for value in values]
    return statistics.fmean(items) if items else 0.0


def _relative_to_root(path: str | Path) -> str:
    resolved = Path(path).resolve()
    try:
        return resolved.relative_to(ROOT_DIR).as_posix()
    except ValueError:
        return resolved.as_posix()


def _render_markdown(report: dict) -> str:
    summary = report["summary"]
    lines = [
        "# P0-4 Domain Extension Evaluation",
        "",
        f"- Created: {report['metadata']['created_at']}",
        f"- Dataset SHA-256: `{report['metadata']['dataset_sha256']}`",
        f"- Cases: {summary['case_count']}",
        "",
        "| Metric | Value |",
        "|---|---:|",
        f"| Lite Top-1 | {summary['lite_top1_accuracy']:.3f} |",
        f"| Lite MRR | {summary['lite_mrr']:.3f} |",
        f"| Reranker Top-1 | {summary['reranker_top1_accuracy']:.3f} |",
        f"| Reranker MRR | {summary['reranker_mrr']:.3f} |",
        f"| Lite average latency | {summary['lite_avg_latency_us']:.2f} us |",
        f"| Reranker average latency | {summary['reranker_avg_latency_us']:.2f} us |",
    ]
    comparison = report.get("comparison")
    if comparison:
        lines.extend(
            [
                "",
                "## Comparison",
                "",
                f"- Passed: {comparison['passed']}",
                "",
                "| Metric | Before | After | Delta |",
                "|---|---:|---:|---:|",
            ]
        )
        for metric in QUALITY_METRICS:
            item = comparison["metrics"][metric]
            lines.append(
                f"| {metric} | {item['before']:.3f} | "
                f"{item['after']:.3f} | {item['delta']:.3f} |"
            )
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Evaluate P0-4 domain-neutral retrieval signals."
    )
    parser.add_argument("--dataset", default=str(DEFAULT_DATASET))
    parser.add_argument("--baseline", default=str(DEFAULT_BASELINE))
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT))
    parser.add_argument("--max-quality-drop", type=float, default=0.01)
    args = parser.parse_args()

    dataset = load_dataset(args.dataset)
    report = evaluate(dataset, args.dataset)
    baseline_path = Path(args.baseline).resolve()
    if baseline_path.exists():
        baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
        report["comparison"] = compare_reports(
            baseline,
            report,
            args.max_quality_drop,
        )
    json_path, markdown_path = save_report(report, args.output_dir)
    comparison = report.get("comparison")
    print(
        json.dumps(
            {
                "passed": comparison["passed"] if comparison else True,
                **report["summary"],
                "json": str(json_path),
                "markdown": str(markdown_path),
            },
            ensure_ascii=True,
        )
    )
    return 0 if comparison is None or comparison["passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
