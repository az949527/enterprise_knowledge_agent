from __future__ import annotations

import argparse
from datetime import datetime
from io import StringIO
import json
from pathlib import Path
import statistics
import sys
from time import perf_counter
import unittest


ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

import fitz

import app.documents.pdf_parser as pdf_parser


DEFAULT_OUTPUT = ROOT_DIR / "outputs" / "evals"
DEFAULT_BASELINE_SECONDS = 1.4154
DEFAULT_BASELINE_FIND_TABLES_CALLS = 8


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run P0-5 PDF structure acceptance and real-PDF benchmark."
    )
    parser.add_argument(
        "--pdf",
        type=Path,
        help="Real PDF fixture. Defaults to the first PDF under data/documents.",
    )
    parser.add_argument("--runs", type=int, default=5)
    parser.add_argument(
        "--baseline-seconds",
        type=float,
        default=DEFAULT_BASELINE_SECONDS,
    )
    parser.add_argument(
        "--baseline-find-tables-calls",
        type=int,
        default=DEFAULT_BASELINE_FIND_TABLES_CALLS,
    )
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    return parser.parse_args()


def resolve_pdf(path: Path | None) -> Path:
    if path is not None:
        resolved = path.resolve()
        if not resolved.exists():
            raise FileNotFoundError(resolved)
        return resolved
    candidates = sorted((ROOT_DIR / "data" / "documents").glob("*.pdf"))
    if not candidates:
        raise FileNotFoundError(
            "No real PDF fixture found. Pass --pdf or add a PDF under data/documents."
        )
    return candidates[0].resolve()


def run_acceptance_tests() -> dict:
    suite = unittest.defaultTestLoader.loadTestsFromName(
        "tests.test_p0_5_acceptance"
    )
    stream = StringIO()
    result = unittest.TextTestRunner(stream=stream, verbosity=2).run(suite)
    return {
        "passed": result.wasSuccessful(),
        "tests_run": result.testsRun,
        "failures": len(result.failures),
        "errors": len(result.errors),
        "output": stream.getvalue(),
    }


def benchmark_pdf(
    path: Path,
    runs: int,
    *,
    force_all_pages: bool,
) -> dict:
    if runs < 1:
        raise ValueError("--runs must be at least 1.")
    original_find_tables = fitz.Page.find_tables
    original_should_probe_tables = pdf_parser._should_probe_tables
    elapsed_values = []
    find_tables_calls = []
    nodes = []
    page_count = 0
    try:
        for _ in range(runs):
            calls = {"count": 0}

            def counted_find_tables(page, *args, **kwargs):
                calls["count"] += 1
                return original_find_tables(page, *args, **kwargs)

            fitz.Page.find_tables = counted_find_tables
            if force_all_pages:
                pdf_parser._should_probe_tables = lambda *_args, **_kwargs: True
            else:
                pdf_parser._should_probe_tables = original_should_probe_tables
            with fitz.open(path) as document:
                page_count = len(document)
                started = perf_counter()
                nodes = list(
                    pdf_parser.iter_pdf_document_nodes(
                        document,
                        document_id="p0_5_real_pdf",
                        source_path=path.name,
                    )
                )
                elapsed_values.append(perf_counter() - started)
            find_tables_calls.append(calls["count"])
    finally:
        fitz.Page.find_tables = original_find_tables
        pdf_parser._should_probe_tables = original_should_probe_tables

    node_types = {
        node_type: sum(node.node_type.value == node_type for node in nodes)
        for node_type in ("text", "table", "figure")
    }
    return {
        "pdf": str(path),
        "pages": page_count,
        "runs": runs,
        "elapsed_seconds": elapsed_values,
        "median_seconds": statistics.median(elapsed_values),
        "min_seconds": min(elapsed_values),
        "find_tables_calls": find_tables_calls,
        "median_find_tables_calls": statistics.median(find_tables_calls),
        "find_tables_page_ratio": (
            statistics.median(find_tables_calls) / page_count
            if page_count
            else 0.0
        ),
        "node_count": len(nodes),
        "node_types": node_types,
        "formula_nodes": sum(
            node.metadata.get("content_kind") == "formula"
            for node in nodes
        ),
        "cross_page_tables": sum(
            bool(node.metadata.get("cross_page"))
            for node in nodes
        ),
        "force_all_pages": force_all_pages,
    }


def benchmark_ab(path: Path, runs: int) -> tuple[dict, dict]:
    if runs < 1:
        raise ValueError("--runs must be at least 1.")
    original_find_tables = fitz.Page.find_tables
    original_should_probe_tables = pdf_parser._should_probe_tables
    records = {
        False: {"elapsed": [], "calls": [], "nodes": [], "pages": 0},
        True: {"elapsed": [], "calls": [], "nodes": [], "pages": 0},
    }
    try:
        for run_index in range(runs):
            modes = (False, True) if run_index % 2 == 0 else (True, False)
            for force_all_pages in modes:
                calls = {"count": 0}

                def counted_find_tables(page, *args, **kwargs):
                    calls["count"] += 1
                    return original_find_tables(page, *args, **kwargs)

                fitz.Page.find_tables = counted_find_tables
                pdf_parser._should_probe_tables = (
                    (lambda *_args, **_kwargs: True)
                    if force_all_pages
                    else original_should_probe_tables
                )
                with fitz.open(path) as document:
                    records[force_all_pages]["pages"] = len(document)
                    started = perf_counter()
                    nodes = list(
                        pdf_parser.iter_pdf_document_nodes(
                            document,
                            document_id=(
                                "p0_5_forced"
                                if force_all_pages
                                else "p0_5_gated"
                            ),
                            source_path=path.name,
                        )
                    )
                    records[force_all_pages]["elapsed"].append(
                        perf_counter() - started
                    )
                records[force_all_pages]["calls"].append(calls["count"])
                records[force_all_pages]["nodes"] = nodes
    finally:
        fitz.Page.find_tables = original_find_tables
        pdf_parser._should_probe_tables = original_should_probe_tables
    return (
        _benchmark_record(path, runs, records[False], False),
        _benchmark_record(path, runs, records[True], True),
    )


def _benchmark_record(
    path: Path,
    runs: int,
    record: dict,
    force_all_pages: bool,
) -> dict:
    nodes = record["nodes"]
    elapsed_values = record["elapsed"]
    find_tables_calls = record["calls"]
    page_count = record["pages"]
    return {
        "pdf": str(path),
        "pages": page_count,
        "runs": runs,
        "elapsed_seconds": elapsed_values,
        "median_seconds": statistics.median(elapsed_values),
        "min_seconds": min(elapsed_values),
        "find_tables_calls": find_tables_calls,
        "median_find_tables_calls": statistics.median(find_tables_calls),
        "find_tables_page_ratio": (
            statistics.median(find_tables_calls) / page_count
            if page_count
            else 0.0
        ),
        "node_count": len(nodes),
        "node_types": {
            node_type: sum(node.node_type.value == node_type for node in nodes)
            for node_type in ("text", "table", "figure")
        },
        "formula_nodes": sum(
            node.metadata.get("content_kind") == "formula"
            for node in nodes
        ),
        "cross_page_tables": sum(
            bool(node.metadata.get("cross_page"))
            for node in nodes
        ),
        "force_all_pages": force_all_pages,
    }


def build_report(
    acceptance: dict,
    benchmark: dict,
    forced_benchmark: dict,
    baseline_seconds: float,
    baseline_find_tables_calls: int,
) -> dict:
    after = benchmark["median_seconds"]
    reduction = (
        (baseline_seconds - after) / baseline_seconds
        if baseline_seconds > 0
        else None
    )
    find_tables_avoidance = 1.0 - benchmark["find_tables_page_ratio"]
    ab_reduction = (
        (
            forced_benchmark["median_seconds"]
            - benchmark["median_seconds"]
        )
        / forced_benchmark["median_seconds"]
        if forced_benchmark["median_seconds"] > 0
        else None
    )
    passed = (
        acceptance["passed"]
        and benchmark["median_find_tables_calls"] < benchmark["pages"]
        and benchmark["median_find_tables_calls"]
        < forced_benchmark["median_find_tables_calls"]
        and benchmark["median_seconds"] <= forced_benchmark["median_seconds"] * 1.1
    )
    return {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "parser_version": pdf_parser.PDF_PARSER_VERSION,
        "passed": passed,
        "acceptance": acceptance,
        "benchmark": benchmark,
        "forced_all_pages_benchmark": forced_benchmark,
        "comparison": {
            "before_seconds": baseline_seconds,
            "after_seconds": after,
            "latency_reduction_ratio": reduction,
            "before_find_tables_calls": baseline_find_tables_calls,
            "after_find_tables_calls": benchmark["median_find_tables_calls"],
            "find_tables_call_reduction_ratio": (
                (
                    baseline_find_tables_calls
                    - benchmark["median_find_tables_calls"]
                )
                / baseline_find_tables_calls
                if baseline_find_tables_calls > 0
                else None
            ),
            "find_tables_avoidance_ratio": find_tables_avoidance,
            "forced_all_pages_seconds": forced_benchmark["median_seconds"],
            "forced_all_pages_calls": forced_benchmark[
                "median_find_tables_calls"
            ],
            "gated_vs_forced_latency_reduction_ratio": ab_reduction,
        },
        "implemented_actions": [
            "Reject numeric-only, formula, DOI, figure-caption, table-caption, and long-sentence headings.",
            "Require persistent parallel columns and a clear gutter; otherwise preserve PyMuPDF source order.",
            "Detect three-line tables and merge compatible cross-page table continuations.",
            "Forward-fill parent header spans and emit complete multi-level column names.",
            "Group spatially adjacent formula fragments into one formula-content text node.",
            "Emit figure nodes with caption, page bbox, visual bbox, caption bbox, and nearby body text.",
            "Reuse the first page layout scan and call find_tables only on suspected table pages.",
        ],
    }


def render_markdown(report: dict) -> str:
    acceptance = report["acceptance"]
    benchmark = report["benchmark"]
    forced_benchmark = report["forced_all_pages_benchmark"]
    comparison = report["comparison"]
    reduction = comparison["latency_reduction_ratio"]
    historical_change = (reduction or 0.0) * 100
    historical_label = (
        f"{historical_change:.1f}% reduction"
        if historical_change >= 0
        else f"{abs(historical_change):.1f}% increase"
    )
    lines = [
        "# P0-5 PDF Structure Evaluation",
        "",
        f"- Created: {report['created_at']}",
        f"- Parser: `{report['parser_version']}`",
        f"- Passed: {report['passed']}",
        f"- Acceptance tests: {acceptance['tests_run']}",
        f"- Real PDF: `{benchmark['pdf']}`",
        f"- Pages: {benchmark['pages']}",
        "",
        "## Implemented Actions",
        "",
    ]
    lines.extend(f"- {action}" for action in report["implemented_actions"])
    lines.extend(
        [
            "",
            "## Comparison",
            "",
            "| Metric | Before | After | Change |",
            "|---|---:|---:|---:|",
            (
                "| Parse median | "
                f"{comparison['before_seconds']:.4f} s | "
                f"{comparison['after_seconds']:.4f} s | "
                f"{historical_label} |"
            ),
            (
                "| find_tables pages | "
                f"{comparison['before_find_tables_calls']}/{benchmark['pages']} | "
                f"{benchmark['median_find_tables_calls']:.0f}/{benchmark['pages']} | "
                f"{(comparison['find_tables_call_reduction_ratio'] or 0.0) * 100:.1f}% fewer calls |"
            ),
            (
                "| Same-code forced-page A/B | "
                f"{forced_benchmark['median_seconds']:.4f} s "
                f"({forced_benchmark['median_find_tables_calls']:.0f}/{benchmark['pages']} calls) | "
                f"{benchmark['median_seconds']:.4f} s "
                f"({benchmark['median_find_tables_calls']:.0f}/{benchmark['pages']} calls) | "
                f"{(comparison['gated_vs_forced_latency_reduction_ratio'] or 0.0) * 100:.1f}% reduction |"
            ),
            "",
            "## Actual Output",
            "",
            "| Node type | Count |",
            "|---|---:|",
            f"| text | {benchmark['node_types']['text']} |",
            f"| table | {benchmark['node_types']['table']} |",
            f"| figure | {benchmark['node_types']['figure']} |",
            f"| formula-content text | {benchmark['formula_nodes']} |",
            f"| total | {benchmark['node_count']} |",
            "",
            "## Runs",
            "",
            "- Seconds: "
            + ", ".join(
                f"{value:.4f}"
                for value in benchmark["elapsed_seconds"]
            ),
            "- find_tables calls: "
            + ", ".join(
                str(value)
                for value in benchmark["find_tables_calls"]
            ),
            "- Forced-page seconds: "
            + ", ".join(
                f"{value:.4f}"
                for value in forced_benchmark["elapsed_seconds"]
            ),
            "- Forced-page find_tables calls: "
            + ", ".join(
                str(value)
                for value in forced_benchmark["find_tables_calls"]
            ),
            "",
        ]
    )
    return "\n".join(lines)


def save_report(report: dict, output_dir: Path) -> tuple[Path, Path]:
    output_dir = output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    json_path = output_dir / f"p0_5_eval_{stamp}.json"
    markdown_path = output_dir / f"p0_5_eval_{stamp}.md"
    json_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    markdown_path.write_text(render_markdown(report), encoding="utf-8")
    return json_path, markdown_path


def main() -> int:
    args = parse_args()
    pdf_path = resolve_pdf(args.pdf)
    acceptance = run_acceptance_tests()
    benchmark, forced_benchmark = benchmark_ab(pdf_path, args.runs)
    report = build_report(
        acceptance,
        benchmark,
        forced_benchmark,
        args.baseline_seconds,
        args.baseline_find_tables_calls,
    )
    json_path, markdown_path = save_report(report, args.output_dir)
    print(
        json.dumps(
            {
                "passed": report["passed"],
                "tests_run": acceptance["tests_run"],
                "median_seconds": benchmark["median_seconds"],
                "find_tables_calls": benchmark["median_find_tables_calls"],
                "pages": benchmark["pages"],
                "json": str(json_path),
                "markdown": str(markdown_path),
            },
            ensure_ascii=False,
        )
    )
    return 0 if report["passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
