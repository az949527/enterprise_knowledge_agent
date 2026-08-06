"""P1-1 评估：父上下文扩展 开/关 对照。

在冻结的 30 条企业制度基线上，同一索引各跑一次：
  - no_parent     ：命中小块直接送生成（P0-8 现状）
  - parent_context：命中小块先扩展到父章节/父表格再送生成

对比答案覆盖率与引用准确率，并做与已提交基线的质量回退门禁。
报告写入 outputs/evals/。

用法：
  .venv-desktop\\Scripts\\python.exe scripts\\eval_p1_1.py [--use-llm] [--api-key KEY]
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime
import json
import statistics
from pathlib import Path
import sys
import tempfile

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.core.config import settings
from app.lite.bm25_search import search_bm25_index
from app.lite.generator import answer_query
from app.lite.indexer import build_index, DEFAULT_INDEX_DIR
from app.lite.parent_context import ParentContextResolver
from scripts.eval_p0_1 import (
    _score_case,
    compare_reports,
    load_dataset,
    load_manifest,
    validate_frozen_inputs,
)


OUTPUT_DIR = ROOT_DIR / "outputs" / "evals"


def _mean(values):
    values = [value for value in values if value is not None]
    return statistics.fmean(values) if values else 0.0


def _summary(cases, label):
    retrieval = [case for case in cases if case["retrieval_expected"]]
    refusals = [case for case in cases if case["expected_refusal"]]
    return {
        "label": label,
        "case_count": len(cases),
        "retrieval_case_count": len(retrieval),
        "recall_at_5": _mean(case["retrieval_hit"] for case in retrieval),
        "mrr": _mean(case["reciprocal_rank"] for case in retrieval),
        "answer_coverage": _mean(case["answer_coverage"] for case in cases),
        "citation_accuracy": _mean(case["citation_accuracy"] for case in cases),
        "refusal_accuracy": (
            _mean(case["refusal_correct"] for case in refusals)
            if refusals
            else None
        ),
    }


async def _evaluate_case(
    item,
    index_dir,
    *,
    top_k,
    use_llm,
    llm_api_key,
    llm_base_url,
    llm_model,
    use_parent,
    max_parent_chars,
    max_total_chars,
):
    sources = search_bm25_index(
        str(item.get("question") or ""),
        index_dir,
        top_k=top_k,
    )
    if use_parent:
        ParentContextResolver(
            index_dir,
            max_parent_chars=max_parent_chars,
            max_total_chars=max_total_chars,
        ).resolve(sources)
    answer = await answer_query(
        str(item.get("question") or ""),
        sources,
        use_llm=use_llm,
        api_key=llm_api_key,
        base_url=llm_base_url,
        model=llm_model,
    )
    return _score_case(item, sources, answer, 0.0)


async def evaluate(
    manifest,
    *,
    top_k,
    use_llm,
    llm_api_key,
    llm_base_url,
    llm_model,
    max_parent_chars,
    max_total_chars,
):
    dataset_path = ROOT_DIR / str(manifest["dataset"])
    documents_dir = ROOT_DIR / str(manifest["documents_dir"])
    dataset = load_dataset(dataset_path)
    validate_frozen_inputs(manifest, dataset_path, documents_dir)

    with tempfile.TemporaryDirectory() as temp_dir:
        index_dir = Path(temp_dir) / "index"
        build_index(
            documents_dir,
            index_dir,
            chunk_size=int(manifest.get("chunk_size") or 900),
            chunk_overlap=int(manifest.get("chunk_overlap") or 120),
        )

        no_parent_cases = []
        parent_cases = []
        for item in dataset:
            no_parent_cases.append(
                await _evaluate_case(
                    item,
                    index_dir,
                    top_k=top_k,
                    use_llm=use_llm,
                    llm_api_key=llm_api_key,
                    llm_base_url=llm_base_url,
                    llm_model=llm_model,
                    use_parent=False,
                    max_parent_chars=max_parent_chars,
                    max_total_chars=max_total_chars,
                )
            )
            parent_cases.append(
                await _evaluate_case(
                    item,
                    index_dir,
                    top_k=top_k,
                    use_llm=use_llm,
                    llm_api_key=llm_api_key,
                    llm_base_url=llm_base_url,
                    llm_model=llm_model,
                    use_parent=True,
                    max_parent_chars=max_parent_chars,
                    max_total_chars=max_total_chars,
                )
            )

    no_parent = _summary(no_parent_cases, "no_parent")
    parent = _summary(parent_cases, "parent_context")
    delta = {
        metric: (
            round((parent.get(metric) or 0) - (no_parent.get(metric) or 0), 4)
            if isinstance(no_parent.get(metric), (int, float))
            and isinstance(parent.get(metric), (int, float))
            else None
        )
        for metric in (
            "recall_at_5",
            "mrr",
            "answer_coverage",
            "citation_accuracy",
            "refusal_accuracy",
        )
    }

    # 与已提交基线做回退门禁：父扩展不得让未扩展路径比基线差。
    gate = None
    baseline_path = ROOT_DIR / str(manifest.get("baseline_report") or "")
    if baseline_path.exists():
        baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
        no_parent_report = {
            "metadata": baseline.get("metadata", {}),
            "summary": {
                **no_parent,
                "top_k": top_k,
                "type_metrics": {},
            },
        }
        gate = compare_reports(baseline, no_parent_report)

    return {
        "metadata": {
            "baseline_id": manifest.get("baseline_id"),
            "dataset": manifest.get("dataset"),
            "dataset_sha256": manifest.get("dataset_sha256"),
            "strategy": "lite_bm25_parent_compare",
            "use_llm": use_llm,
            "model": llm_model or settings.LLM_MODEL,
            "top_k": top_k,
            "max_parent_chars": max_parent_chars,
            "max_total_chars": max_total_chars,
            "git_commit": _git_commit(),
        },
        "summary": {
            "no_parent": no_parent,
            "parent_context": parent,
            "delta_parent_minus_no_parent": delta,
        },
        "quality_gate_vs_baseline": gate,
        "cases": {
            "no_parent": no_parent_cases,
            "parent_context": parent_cases,
        },
    }


def _git_commit() -> str:
    import subprocess

    try:
        return subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
        ).stdout.strip()
    except OSError:
        return ""


def _write_reports(report):
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    json_path = OUTPUT_DIR / f"p1_1_eval_{timestamp}.json"
    json_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    md_path = OUTPUT_DIR / f"p1_1_eval_{timestamp}.md"
    summary = report["summary"]
    rows = []
    for metric in ("recall_at_5", "mrr", "answer_coverage", "citation_accuracy"):
        no_parent = summary["no_parent"].get(metric)
        parent = summary["parent_context"].get(metric)
        delta = summary["delta_parent_minus_no_parent"].get(metric)
        rows.append(
            f"| {metric} | {no_parent} | {parent} | {delta} |"
        )
    md_path.write_text(
        "# P1-1 Parent-Child 自适应检索评估\n\n"
        f"- baseline: {report['metadata']['baseline_id']}\n"
        f"- dataset SHA-256: {report['metadata']['dataset_sha256']}\n"
        f"- use_llm: {report['metadata']['use_llm']} / model: {report['metadata']['model']}\n"
        f"- top_k: {report['metadata']['top_k']}\n\n"
        "| 指标 | 无父扩展 | 父上下文 | Δ |\n"
        "|---|---:|---:|---:|\n"
        + "\n".join(rows)
        + "\n\n质量回退门禁: "
        + ("通过" if report.get("quality_gate_vs_baseline", {}).get("passed") else "未通过")
        + "\n",
        encoding="utf-8",
    )
    return json_path, md_path


def _parse_args():
    parser = argparse.ArgumentParser(description="P1-1 父上下文扩展评估")
    parser.add_argument("--use-llm", action="store_true", help="使用 LLM 生成答案")
    parser.add_argument("--api-key", default=settings.LLM_API_KEY or "")
    parser.add_argument("--base-url", default=settings.LLM_BASE_URL or "")
    parser.add_argument("--model", default=settings.LLM_MODEL or "")
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument(
        "--max-parent-chars",
        type=int,
        default=settings.PARENT_CONTEXT_MAX_PARENT_CHARS,
    )
    parser.add_argument(
        "--max-total-chars",
        type=int,
        default=settings.PARENT_CONTEXT_MAX_TOTAL_CHARS,
    )
    return parser.parse_args()


def main():
    args = _parse_args()
    manifest = load_manifest()
    report = asyncio.run(
        evaluate(
            manifest,
            top_k=args.top_k,
            use_llm=args.use_llm,
            llm_api_key=args.api_key,
            llm_base_url=args.base_url,
            llm_model=args.model,
            max_parent_chars=args.max_parent_chars,
            max_total_chars=args.max_total_chars,
        )
    )
    json_path, md_path = _write_reports(report)
    summary = report["summary"]
    print("no_parent  :", json.dumps(summary["no_parent"], ensure_ascii=False))
    print("parent     :", json.dumps(summary["parent_context"], ensure_ascii=False))
    print("delta      :", json.dumps(summary["delta_parent_minus_no_parent"], ensure_ascii=False))
    print("gate       :", json.dumps(report.get("quality_gate_vs_baseline"), ensure_ascii=False))
    print("report     :", json_path, "/", md_path)


if __name__ == "__main__":
    main()
