from __future__ import annotations

import argparse
import asyncio
import ctypes
from dataclasses import asdict, is_dataclass
from datetime import datetime
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import statistics
import subprocess
import sys
import tempfile
import threading
from time import perf_counter, sleep
from typing import Any, Iterable, Union


ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.lite.bm25_search import search_bm25_index
from app.lite.generator import answer_query
from app.lite.indexer import IndexFormatError, build_index, ensure_index_format


DEFAULT_MANIFEST = ROOT_DIR / "evals" / "p0_1_baseline_manifest.json"
DEFAULT_INDEX = ROOT_DIR / "data" / "p0_1_eval_index"
DEFAULT_OUTPUT = ROOT_DIR / "outputs" / "evals"
DEFAULT_TOP_K = 5
QUALITY_METRICS = (
    "recall_at_5",
    "mrr",
    "answer_coverage",
    "citation_accuracy",
)
COMPARISON_METRICS = QUALITY_METRICS + (
    "avg_latency_ms",
    "p95_latency_ms",
    "index_elapsed_ms",
    "index_peak_memory_bytes",
    "index_disk_bytes",
    "api_tokens",
    "api_cost_usd",
)
PathLike = Union[str, Path]


class DatasetValidationError(ValueError):
    pass


class RssSampler:
    def __init__(self, interval_seconds=0.01):
        self.interval_seconds = interval_seconds
        self.start_bytes = 0
        self.peak_bytes = 0
        self.method = "unavailable"
        self._stop = threading.Event()
        self._thread = None

    def __enter__(self):
        self.start_bytes, self.method = current_rss_bytes()
        self.peak_bytes = self.start_bytes
        self._thread = threading.Thread(target=self._sample, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, exc_type, exc, traceback):
        self._stop.set()
        self._thread.join(timeout=1)
        current, _ = current_rss_bytes()
        self.peak_bytes = max(self.peak_bytes, current)

    @property
    def delta_bytes(self):
        return max(self.peak_bytes - self.start_bytes, 0)

    def _sample(self):
        while not self._stop.is_set():
            current, _ = current_rss_bytes()
            self.peak_bytes = max(self.peak_bytes, current)
            sleep(self.interval_seconds)


def load_manifest(path: PathLike = DEFAULT_MANIFEST):
    value = json.loads(Path(path).resolve().read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise DatasetValidationError("P0-1 manifest must be a JSON object.")
    return value


def load_dataset(path: PathLike):
    value = json.loads(Path(path).resolve().read_text(encoding="utf-8"))
    if not isinstance(value, list) or not value:
        raise DatasetValidationError("P0-1 dataset must be a non-empty array.")
    validate_cases(value)
    return value


def validate_cases(cases):
    seen = set()
    for index, case in enumerate(cases):
        if not isinstance(case, dict):
            raise DatasetValidationError(
                "Dataset case at index {0} must be an object.".format(index)
            )
        case_id = str(case.get("id") or "").strip()
        question = str(case.get("question") or "").strip()
        if not case_id or not question:
            raise DatasetValidationError(
                "Dataset case at index {0} requires id and question.".format(index)
            )
        if case_id in seen:
            raise DatasetValidationError("Duplicate case id: {0}".format(case_id))
        seen.add(case_id)


def load_ready_extension_cases(path: PathLike):
    cases = load_dataset(path)
    statuses = {}
    ready = []
    for case in cases:
        status = str(case.get("status") or "ready")
        statuses[status] = statuses.get(status, 0) + 1
        if status == "ready":
            ready.append(case)
    return ready, statuses


def build_extension_corpus(documents_dir: PathLike, extension_cases, target: Path) -> Path:
    """把冻结语料与扩展夹具复制到临时目录，用于扩展用例评分。"""
    target = Path(target).resolve()
    target.mkdir(parents=True, exist_ok=True)
    source_root = Path(documents_dir).resolve()
    for source in sorted(source_root.iterdir()):
        if source.is_file():
            shutil.copy2(source, target / source.name)
    for case in extension_cases:
        fixture = case.get("fixture")
        if not fixture:
            continue
        fixture_path = resolve_root_path(fixture)
        if fixture_path.is_file():
            shutil.copy2(fixture_path, target / fixture_path.name)
    return target


def validate_frozen_inputs(manifest, dataset_path, documents_dir):
    dataset_path = Path(dataset_path).resolve()
    documents_dir = Path(documents_dir).resolve()
    dataset = load_dataset(dataset_path)
    dataset_hash = sha256_file(dataset_path)
    documents_hash = sha256_directory(documents_dir)
    errors = []
    if len(dataset) != int(manifest.get("case_count") or 0):
        errors.append("case count changed")
    if dataset_hash != str(manifest.get("dataset_sha256") or "").lower():
        errors.append("dataset SHA-256 changed")
    if documents_hash != str(manifest.get("documents_sha256") or "").lower():
        errors.append("document corpus SHA-256 changed")
    if errors:
        raise DatasetValidationError(
            "Frozen P0-1 inputs do not match the manifest: " + "; ".join(errors)
        )
    return {
        "dataset_sha256": dataset_hash,
        "documents_sha256": documents_hash,
        "case_ids_sha256": case_ids_sha256(dataset),
    }


async def evaluate_async(
    dataset,
    *,
    documents_dir,
    index_dir,
    baseline_id,
    dataset_path,
    dataset_hash,
    documents_hash,
    top_k=DEFAULT_TOP_K,
    use_llm=False,
    llm_api_key="",
    llm_base_url="",
    llm_model="",
    input_cost_per_million=None,
    output_cost_per_million=None,
    extension_statuses=None,
):
    index_path = Path(index_dir).resolve()
    if (index_path / "manifest.json").exists():
        try:
            ensure_index_format(index_path)
        except IndexFormatError:
            shutil.rmtree(index_path, ignore_errors=True)
    reset_bm25_cache(index_path)
    warmup_query = next(
        (
            str(case.get("question") or "")
            for case in dataset
            if not case.get("expected_refusal")
        ),
        "",
    )
    with RssSampler() as index_memory:
        started = perf_counter()
        index_stats = build_index(documents_dir, index_path)
        if warmup_query:
            search_bm25_index(warmup_query, index_path, top_k=top_k)
        index_elapsed_ms = (perf_counter() - started) * 1000

    cases = []
    latencies = []
    usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    with RssSampler() as query_memory:
        for item in dataset:
            started = perf_counter()
            sources = search_bm25_index(
                str(item.get("question") or ""), index_path, top_k=top_k
            )
            answer = await answer_query(
                str(item.get("question") or ""),
                sources,
                use_llm=use_llm,
                api_key=llm_api_key,
                base_url=llm_base_url,
                model=llm_model,
            )
            elapsed_ms = (perf_counter() - started) * 1000
            latencies.append(elapsed_ms)
            accumulate_usage(usage, (answer.get("llm") or {}).get("usage"))
            cases.append(_score_case(item, sources, answer, elapsed_ms))

    retrieval = [case for case in cases if case["retrieval_expected"]]
    refusals = [case for case in cases if case["expected_refusal"]]
    summary = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "strategy": "lite_bm25_plus_llm" if use_llm else "lite_bm25_local",
        "case_count": len(cases),
        "retrieval_case_count": len(retrieval),
        "refusal_case_count": len(refusals),
        "top_k": top_k,
        "recall_at_5": mean(case["retrieval_hit"] for case in retrieval),
        "mrr": mean(case["reciprocal_rank"] for case in retrieval),
        "answer_coverage": mean(case["answer_coverage"] for case in cases),
        "citation_accuracy": mean(case["citation_accuracy"] for case in cases),
        "refusal_accuracy": (
            mean(case["refusal_correct"] for case in refusals)
            if refusals
            else None
        ),
        "avg_latency_ms": statistics.fmean(latencies) if latencies else 0.0,
        "p95_latency_ms": percentile(latencies, 0.95),
        "index_elapsed_ms": index_elapsed_ms,
        "index_peak_memory_bytes": index_memory.peak_bytes,
        "index_memory_delta_bytes": index_memory.delta_bytes,
        "query_peak_memory_bytes": query_memory.peak_bytes,
        "query_memory_delta_bytes": query_memory.delta_bytes,
        "memory_measurement": index_memory.method,
        "index_disk_bytes": sum(
            path.stat().st_size
            for path in index_path.rglob("*")
            if path.is_file()
        ),
        "api_tokens": usage["total_tokens"],
        "api_prompt_tokens": usage["prompt_tokens"],
        "api_completion_tokens": usage["completion_tokens"],
        "api_cost_usd": calculate_api_cost(
            usage,
            use_llm=use_llm,
            input_cost_per_million=input_cost_per_million,
            output_cost_per_million=output_cost_per_million,
        ),
        "api_pricing": {
            "input_cost_per_million_usd": input_cost_per_million,
            "output_cost_per_million_usd": output_cost_per_million,
        },
        "index_stats": to_dict(index_stats),
        "type_metrics": summarize_by_type(cases),
        "extension_statuses": extension_statuses or {},
    }
    metadata = {
        "baseline_id": baseline_id,
        "dataset": relative_to_root(dataset_path),
        "dataset_sha256": dataset_hash,
        "documents_dir": relative_to_root(documents_dir),
        "documents_sha256": documents_hash,
        "case_ids_sha256": case_ids_sha256(dataset),
        "git_commit": git_commit(),
        "python_version": sys.version.split()[0],
        "platform": sys.platform,
    }
    return {"metadata": metadata, "summary": summary, "cases": cases}


def evaluate(dataset, **kwargs):
    return asyncio.run(evaluate_async(dataset, **kwargs))


def _score_case(item, sources, answer, elapsed_ms):
    expected_document = str(item.get("expected_document_contains") or "")
    expected_chunks = set(item.get("expected_chunk_indices") or [])
    expected_refusal = bool(item.get("expected_refusal"))
    retrieval_expected = not expected_refusal and bool(
        expected_document or expected_chunks
    )
    hit_rank = None
    if retrieval_expected:
        for rank, source in enumerate(sources, start=1):
            if source_matches_expected(source, expected_document, expected_chunks):
                hit_rank = rank
                break

    answer_text = str(answer.get("answer") or "")
    coverage = term_coverage(
        answer_text, item.get("expected_answer_terms") or []
    )
    citations = extract_citations(answer_text)
    citation_accuracy = _citation_accuracy(
        citations,
        sources,
        expected_document,
        expected_chunks,
        expected_refusal=expected_refusal,
    )
    refusal_correct = None
    if expected_refusal:
        refusal_correct = coverage >= 1.0 and not citations
    return {
        "id": item.get("id"),
        "type": item.get("type", "plain_text"),
        "question": item.get("question"),
        "reference_answer": item.get("reference_answer"),
        "answer": answer_text,
        "elapsed_ms": elapsed_ms,
        "retrieval_expected": retrieval_expected,
        "retrieval_hit": hit_rank is not None,
        "hit_rank": hit_rank,
        "reciprocal_rank": 1.0 / hit_rank if hit_rank else 0.0,
        "answer_coverage": coverage,
        "citations": citations,
        "citation_accuracy": citation_accuracy,
        "expected_refusal": expected_refusal,
        "refusal_correct": refusal_correct,
        "answer_mode": answer.get("mode"),
        "llm": answer.get("llm"),
        "top_results": [
            {
                "rank": source.get("rank"),
                "filename": source.get("filename"),
                "chunk_index": source.get("chunk_index"),
                "score": source.get("score"),
            }
            for source in sources
        ],
    }


def source_matches_expected(source, expected_document, expected_chunks):
    filename = str(source.get("filename") or "")
    if expected_document and expected_document not in filename:
        return False
    if expected_chunks and source.get("chunk_index") not in expected_chunks:
        return False
    return bool(expected_document or expected_chunks)


def term_coverage(text, terms):
    terms = list(terms)
    if not terms:
        return 1.0
    normalized = normalize(text)
    hits = 0
    for term in terms:
        alternatives = term if isinstance(term, list) else [term]
        if any(normalize(value) in normalized for value in alternatives):
            hits += 1
    return hits / len(terms)


def _citation_accuracy(
    citations,
    sources,
    expected_document,
    expected_chunks,
    *,
    expected_refusal=False,
):
    if expected_refusal:
        return 1.0 if not citations else 0.0
    if not citations:
        return 0.0
    correct = 0
    for citation in citations:
        if 1 <= citation <= len(sources) and source_matches_expected(
            sources[citation - 1], expected_document, expected_chunks
        ):
            correct += 1
    return correct / len(citations)


def extract_citations(answer):
    import re

    return [int(value) for value in re.findall(r"\[(\d+)\]", answer)]


def normalize(value):
    return "".join(
        character.casefold()
        for character in str(value)
        if character.isalnum() or "\u4e00" <= character <= "\u9fff"
    )


def summarize_by_type(cases):
    grouped = {}
    for case in cases:
        grouped.setdefault(str(case.get("type") or "plain_text"), []).append(case)
    result = {}
    for case_type, items in sorted(grouped.items()):
        retrieval = [item for item in items if item["retrieval_expected"]]
        refusals = [item for item in items if item["expected_refusal"]]
        result[case_type] = {
            "case_count": len(items),
            "recall_at_5": (
                mean(item["retrieval_hit"] for item in retrieval)
                if retrieval
                else None
            ),
            "mrr": (
                mean(item["reciprocal_rank"] for item in retrieval)
                if retrieval
                else None
            ),
            "answer_coverage": mean(item["answer_coverage"] for item in items),
            "citation_accuracy": mean(
                item["citation_accuracy"] for item in items
            ),
            "refusal_accuracy": (
                mean(item["refusal_correct"] for item in refusals)
                if refusals
                else None
            ),
        }
    return result


def compare_reports(before, after, *, max_quality_drop=0.01):
    old_summary = before["summary"]
    new_summary = after["summary"]
    failures = []
    for key in ("baseline_id", "dataset_sha256", "case_ids_sha256"):
        old = str((before.get("metadata") or {}).get(key) or "")
        new = str((after.get("metadata") or {}).get(key) or "")
        if old != new:
            failures.append(
                "{0} mismatch: before={1}, after={2}".format(key, old, new)
            )
    for key in ("case_count", "top_k"):
        if old_summary.get(key) != new_summary.get(key):
            failures.append("{0} mismatch".format(key))

    metrics = {}
    for metric in COMPARISON_METRICS:
        metrics[metric] = metric_comparison(old_summary, new_summary, metric)
        delta = metrics[metric]["delta"]
        if (
            metric in QUALITY_METRICS
            and delta is not None
            and delta < -max_quality_drop
        ):
            failures.append(
                "{0} dropped by {1:.4f}; limit is {2:.4f}".format(
                    metric, abs(delta), max_quality_drop
                )
            )

    type_quality = {}
    old_types = old_summary.get("type_metrics") or {}
    new_types = new_summary.get("type_metrics") or {}
    for case_type in sorted(set(old_types).intersection(new_types)):
        old_type = old_types[case_type]
        new_type = new_types[case_type]
        if old_type.get("case_count") != new_type.get("case_count"):
            failures.append("case count mismatch for type {0}".format(case_type))
            continue
        type_quality[case_type] = {}
        for metric in QUALITY_METRICS:
            item = metric_comparison(old_type, new_type, metric)
            type_quality[case_type][metric] = item
            if item["delta"] is not None and item["delta"] < -max_quality_drop:
                failures.append(
                    "{0}.{1} dropped by {2:.4f}; limit is {3:.4f}".format(
                        case_type, metric, abs(item["delta"]), max_quality_drop
                    )
                )
    return {
        "passed": not failures,
        "max_quality_drop": max_quality_drop,
        "metrics": metrics,
        "type_quality": type_quality,
        "failures": failures,
    }


def metric_comparison(before, after, key):
    old = optional_float(before.get(key))
    new = optional_float(after.get(key))
    return {
        "before": old,
        "after": new,
        "delta": None if old is None or new is None else new - old,
    }


def save_report(report, output_dir, *, comparison=None):
    output_dir = Path(output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    json_path = output_dir / "p0_1_eval_{0}.json".format(stamp)
    markdown_path = output_dir / "p0_1_eval_{0}.md".format(stamp)
    payload = dict(report)
    if comparison is not None:
        payload["comparison"] = comparison
    write_report_files(payload, json_path, markdown_path)
    return json_path, markdown_path


def write_baseline_report(report, manifest, *, force=False):
    json_path = resolve_root_path(manifest["baseline_report"])
    markdown_path = resolve_root_path(manifest["baseline_markdown"])
    if not force and (json_path.exists() or markdown_path.exists()):
        raise FileExistsError(
            "Baseline exists. Use --force-baseline to replace it."
        )
    json_path.parent.mkdir(parents=True, exist_ok=True)
    write_report_files(report, json_path, markdown_path)
    return json_path, markdown_path


def write_report_files(report, json_path, markdown_path):
    json_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    markdown_path.write_text(render_markdown(report), encoding="utf-8")


def render_markdown(report):
    summary = report["summary"]
    metadata = report.get("metadata") or {}
    lines = [
        "# P0-1 Evaluation Report",
        "",
        "- Baseline ID: {0}".format(metadata.get("baseline_id")),
        "- Created: {0}".format(summary["created_at"]),
        "- Strategy: {0}".format(summary["strategy"]),
        "- Dataset SHA-256: `{0}`".format(metadata.get("dataset_sha256")),
        "- Document corpus SHA-256: `{0}`".format(
            metadata.get("documents_sha256")
        ),
        "- Cases: {0}".format(summary["case_count"]),
        "",
        "## Quality",
        "",
        "| Metric | Value |",
        "|---|---:|",
        "| Recall@5 | {0:.3f} |".format(summary["recall_at_5"]),
        "| MRR | {0:.3f} |".format(summary["mrr"]),
        "| Answer coverage | {0:.3f} |".format(summary["answer_coverage"]),
        "| Citation accuracy | {0:.3f} |".format(
            summary["citation_accuracy"]
        ),
        "| Refusal accuracy | {0} |".format(
            format_optional_float(summary.get("refusal_accuracy"))
        ),
        "",
        "## Performance And Cost",
        "",
        "| Metric | Value |",
        "|---|---:|",
        "| Average latency | {0:.2f} ms |".format(summary["avg_latency_ms"]),
        "| P95 latency | {0:.2f} ms |".format(summary["p95_latency_ms"]),
        "| Index elapsed | {0:.2f} ms |".format(summary["index_elapsed_ms"]),
        "| Index peak RSS | {0} bytes |".format(
            summary["index_peak_memory_bytes"]
        ),
        "| Index RSS increase | {0} bytes |".format(
            summary["index_memory_delta_bytes"]
        ),
        "| Query peak RSS | {0} bytes |".format(
            summary["query_peak_memory_bytes"]
        ),
        "| Index disk | {0} bytes |".format(summary["index_disk_bytes"]),
        "| API tokens | {0} |".format(summary["api_tokens"]),
        "| API cost | {0} USD |".format(
            format_optional_float(summary.get("api_cost_usd"), digits=6)
        ),
    ]
    comparison = report.get("comparison")
    if comparison is not None:
        lines.extend(
            [
                "",
                "## Acceptance Gate",
                "",
                "- Passed: {0}".format(comparison["passed"]),
                "- Maximum quality drop: {0:.4f}".format(
                    comparison["max_quality_drop"]
                ),
                "",
                "| Metric | Before | After | Delta |",
                "|---|---:|---:|---:|",
            ]
        )
        for metric in COMPARISON_METRICS:
            item = comparison["metrics"][metric]
            lines.append(
                "| {0} | {1} | {2} | {3} |".format(
                    metric,
                    format_optional_float(item["before"], digits=4),
                    format_optional_float(item["after"], digits=4),
                    format_optional_float(item["delta"], digits=4),
                )
            )
        if comparison["failures"]:
            lines.extend(["", "### Failures", ""])
            lines.extend("- {0}".format(item) for item in comparison["failures"])
    lines.extend(
        [
            "",
            "## Case Types",
            "",
            "| Type | Cases | Recall@5 | MRR | Coverage | Citation | Refusal |",
            "|---|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for case_type, metrics in sorted(summary["type_metrics"].items()):
        lines.append(
            "| {0} | {1} | {2} | {3} | {4} | {5} | {6} |".format(
                case_type,
                metrics["case_count"],
                format_optional_float(metrics["recall_at_5"]),
                format_optional_float(metrics["mrr"]),
                format_optional_float(metrics["answer_coverage"]),
                format_optional_float(metrics["citation_accuracy"]),
                format_optional_float(metrics["refusal_accuracy"]),
            )
        )
    return "\n".join(lines) + "\n"


def calculate_api_cost(
    usage,
    *,
    use_llm,
    input_cost_per_million,
    output_cost_per_million,
):
    if not use_llm:
        return 0.0
    if input_cost_per_million is None or output_cost_per_million is None:
        return None
    return (
        usage["prompt_tokens"] * input_cost_per_million
        + usage["completion_tokens"] * output_cost_per_million
    ) / 1_000_000


def accumulate_usage(target, usage):
    if not isinstance(usage, dict):
        return
    for key in target:
        value = usage.get(key)
        if isinstance(value, (int, float)):
            target[key] += int(value)


def mean(values):
    values = [float(value) for value in values]
    return statistics.fmean(values) if values else 0.0


def percentile(values, quantile):
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = max(math.ceil(quantile * len(ordered)), 1)
    return ordered[min(rank - 1, len(ordered) - 1)]


def optional_float(value):
    return None if value is None else float(value)


def format_optional_float(value, *, digits=3):
    if value is None:
        return "not measured"
    return ("{0:." + str(digits) + "f}").format(float(value))


def sha256_file(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def reset_bm25_cache(index_dir):
    index_dir = Path(index_dir).resolve()
    for name in (
        "bm25_index.sqlite3",
        "bm25_index.sqlite3-shm",
        "bm25_index.sqlite3-wal",
        "bm25_index.sqlite3.tmp",
    ):
        path = index_dir / name
        if path.exists():
            path.unlink()


def sha256_directory(path):
    directory = Path(path).resolve()
    if not directory.is_dir():
        raise FileNotFoundError(
            "Document directory does not exist: {0}".format(directory)
        )
    digest = hashlib.sha256()
    for file_path in sorted(
        item for item in directory.rglob("*") if item.is_file()
    ):
        digest.update(file_path.relative_to(directory).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(file_path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def case_ids_sha256(cases):
    digest = hashlib.sha256()
    for case in cases:
        digest.update(str(case.get("id") or "").encode("utf-8"))
        digest.update(b"\0")
    return digest.hexdigest()


def current_rss_bytes():
    try:
        import psutil

        return int(psutil.Process(os.getpid()).memory_info().rss), "psutil_rss"
    except (ImportError, OSError):
        pass
    if sys.platform == "win32":
        return windows_rss_bytes(), "windows_working_set"
    if sys.platform.startswith("linux"):
        try:
            fields = Path("/proc/self/statm").read_text(
                encoding="ascii"
            ).split()
            return int(fields[1]) * int(os.sysconf("SC_PAGE_SIZE")), "proc_statm_rss"
        except (OSError, IndexError, ValueError):
            return 0, "unavailable"
    try:
        import resource

        peak = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
        multiplier = 1 if sys.platform == "darwin" else 1024
        return peak * multiplier, "resource_peak_rss"
    except (ImportError, OSError, ValueError):
        return 0, "unavailable"


def windows_rss_bytes():
    class Counters(ctypes.Structure):
        _fields_ = [
            ("cb", ctypes.c_ulong),
            ("PageFaultCount", ctypes.c_ulong),
            ("PeakWorkingSetSize", ctypes.c_size_t),
            ("WorkingSetSize", ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
            ("PagefileUsage", ctypes.c_size_t),
            ("PeakPagefileUsage", ctypes.c_size_t),
        ]

    counters = Counters()
    counters.cb = ctypes.sizeof(counters)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    kernel32.GetCurrentProcess.argtypes = []
    kernel32.GetCurrentProcess.restype = ctypes.c_void_p
    psapi.GetProcessMemoryInfo.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(Counters),
        ctypes.c_ulong,
    ]
    psapi.GetProcessMemoryInfo.restype = ctypes.c_int
    success = psapi.GetProcessMemoryInfo(
        kernel32.GetCurrentProcess(),
        ctypes.byref(counters),
        ctypes.sizeof(counters),
    )
    return int(counters.WorkingSetSize) if success else 0


def to_dict(value):
    if is_dataclass(value):
        return asdict(value)
    if hasattr(value, "__dict__"):
        return dict(value.__dict__)
    return {"value": str(value)}


def git_commit():
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=str(ROOT_DIR), text=True
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        return ""


def resolve_root_path(path):
    path = Path(path).expanduser()
    return path.resolve() if path.is_absolute() else (ROOT_DIR / path).resolve()


def relative_to_root(path):
    path = Path(path).resolve()
    try:
        return path.relative_to(ROOT_DIR).as_posix()
    except ValueError:
        return path.as_posix()


def main():
    parser = argparse.ArgumentParser(
        description="Run the frozen P0-1 regression baseline and gate."
    )
    parser.add_argument("--manifest", default=str(DEFAULT_MANIFEST))
    parser.add_argument("--dataset")
    parser.add_argument("--documents-dir")
    parser.add_argument("--index-dir", default=str(DEFAULT_INDEX))
    parser.add_argument("--output-dir", default=str(DEFAULT_OUTPUT))
    parser.add_argument("--baseline-report")
    parser.add_argument("--max-quality-drop", type=float)
    parser.add_argument("--top-k", type=int, default=DEFAULT_TOP_K)
    parser.add_argument("--include-extensions", action="store_true")
    parser.add_argument("--use-llm", action="store_true")
    parser.add_argument("--llm-api-key", default="")
    parser.add_argument("--llm-base-url", default="")
    parser.add_argument("--llm-model", default="")
    parser.add_argument("--input-cost-per-million", type=float)
    parser.add_argument("--output-cost-per-million", type=float)
    parser.add_argument("--write-baseline", action="store_true")
    parser.add_argument("--force-baseline", action="store_true")
    args = parser.parse_args()
    if args.top_k != DEFAULT_TOP_K:
        parser.error("P0-1 freezes Recall@5; --top-k must remain 5.")

    manifest = load_manifest(args.manifest)
    dataset_path = resolve_root_path(args.dataset or manifest["dataset"])
    documents_dir = resolve_root_path(
        args.documents_dir or manifest["documents_dir"]
    )
    frozen = validate_frozen_inputs(manifest, dataset_path, documents_dir)
    dataset = load_dataset(dataset_path)
    extension_statuses = {}
    extension_corpus = None
    corpus_cleanup = None
    if args.include_extensions:
        ready, extension_statuses = load_ready_extension_cases(
            resolve_root_path(manifest["extension_dataset"])
        )
        dataset.extend(ready)
        if ready and any(case.get("fixture") for case in ready):
            corpus_cleanup = tempfile.TemporaryDirectory(prefix="p0_1_ext_corpus_")
            extension_corpus = build_extension_corpus(
                documents_dir,
                ready,
                Path(corpus_cleanup.name) / "documents",
            )
            frozen["documents_sha256"] = sha256_directory(extension_corpus)

    index_corpus = extension_corpus or documents_dir
    try:
        report = evaluate(
            dataset,
            documents_dir=index_corpus,
            index_dir=args.index_dir,
            baseline_id=str(manifest["baseline_id"]),
            dataset_path=dataset_path,
            dataset_hash=frozen["dataset_sha256"],
            documents_hash=frozen["documents_sha256"],
            top_k=args.top_k,
            use_llm=args.use_llm,
            llm_api_key=args.llm_api_key,
            llm_base_url=args.llm_base_url,
            llm_model=args.llm_model,
            input_cost_per_million=args.input_cost_per_million,
            output_cost_per_million=args.output_cost_per_million,
            extension_statuses=extension_statuses,
        )
    finally:
        if corpus_cleanup is not None:
            corpus_cleanup.cleanup()

    comparison = None
    if args.write_baseline:
        json_path, markdown_path = write_baseline_report(
            report, manifest, force=args.force_baseline
        )
    else:
        baseline_path = resolve_root_path(
            args.baseline_report or manifest["baseline_report"]
        )
        if baseline_path.exists() and (
            not args.include_extensions or args.baseline_report
        ):
            baseline = json.loads(
                baseline_path.read_text(encoding="utf-8")
            )
            max_drop = (
                args.max_quality_drop
                if args.max_quality_drop is not None
                else float(
                    manifest["acceptance_gate"]["max_absolute_quality_drop"]
                )
            )
            comparison = compare_reports(
                baseline, report, max_quality_drop=max_drop
            )
        json_path, markdown_path = save_report(
            report, args.output_dir, comparison=comparison
        )

    print(
        json.dumps(
            {
                "passed": comparison["passed"] if comparison else True,
                "case_count": report["summary"]["case_count"],
                "recall_at_5": report["summary"]["recall_at_5"],
                "mrr": report["summary"]["mrr"],
                "answer_coverage": report["summary"]["answer_coverage"],
                "citation_accuracy": report["summary"]["citation_accuracy"],
                "json": str(json_path),
                "markdown": str(markdown_path),
            },
            ensure_ascii=True,
        )
    )
    return 0 if comparison is None or comparison["passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
