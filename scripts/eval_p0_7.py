from __future__ import annotations

import argparse
from datetime import datetime
from io import StringIO
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest


ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.lite.index_diagnostics import diagnose_index
from app.lite.indexer import build_index


DEFAULT_OUTPUT = ROOT_DIR / "outputs" / "evals"
DEFAULT_FIXTURE_MIB = 10


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run P0-7 incremental indexing and recovery acceptance."
    )
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--fixture-mib", type=int, default=DEFAULT_FIXTURE_MIB)
    return parser.parse_args()


def run_acceptance_tests() -> dict:
    suite = unittest.defaultTestLoader.discover(
        str(ROOT_DIR / "tests"),
        pattern="test_p0_7_acceptance.py",
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


def create_fixture(source_dir: Path, fixture_mib: int) -> tuple[Path, Path]:
    source_dir.mkdir(parents=True, exist_ok=True)
    large_path = source_dir / "large_policy.txt"
    target_bytes = fixture_mib * 1024 * 1024
    line = "enterprise indexing reliability acceptance line\n"
    with large_path.open("w", encoding="utf-8") as writer:
        while writer.tell() < target_bytes:
            writer.write(line * 2000)
    changing_path = source_dir / "changing_policy.txt"
    changing_path.write_text("policy version one", encoding="utf-8")
    return large_path, changing_path


def timed_build(source_dir: Path, index_dir: Path) -> tuple[dict, float]:
    started = time.perf_counter()
    stats = build_index(source_dir, index_dir)
    elapsed = time.perf_counter() - started
    return stats.__dict__, elapsed


def run_benchmark(fixture_mib: int) -> dict:
    with tempfile.TemporaryDirectory(prefix="p0_7_eval_") as temp_dir:
        root = Path(temp_dir)
        source_dir = root / "documents"
        index_dir = root / "index"
        _large_path, changing_path = create_fixture(source_dir, fixture_mib)

        initial, initial_seconds = timed_build(source_dir, index_dir)
        unchanged, unchanged_seconds = timed_build(source_dir, index_dir)
        changing_path.write_text("policy version two", encoding="utf-8")
        updated, updated_seconds = timed_build(source_dir, index_dir)
        diagnosis = diagnose_index(index_dir)

    passed = (
        initial["added_count"] == 2
        and unchanged["skipped_count"] == 2
        and unchanged["added_count"] == 0
        and unchanged["updated_count"] == 0
        and updated["updated_count"] == 1
        and updated["skipped_count"] == 1
        and diagnosis["status"] == "healthy"
    )
    return {
        "passed": passed,
        "fixture_mib": fixture_mib,
        "initial": {
            "seconds": initial_seconds,
            "stats": initial,
        },
        "unchanged": {
            "seconds": unchanged_seconds,
            "stats": unchanged,
        },
        "single_file_update": {
            "seconds": updated_seconds,
            "stats": updated,
        },
        "diagnosis": diagnosis,
    }


def write_report(report: dict, output_dir: Path) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    json_path = output_dir / f"p0_7_eval_{timestamp}.json"
    markdown_path = output_dir / f"p0_7_eval_{timestamp}.md"
    json_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    benchmark = report["benchmark"]
    lines = [
        "# P0-7 Incremental Indexing Acceptance",
        "",
        f"- Result: {'PASS' if report['passed'] else 'FAIL'}",
        f"- Acceptance tests: {report['acceptance']['tests_run']}",
        f"- Fixture: {benchmark['fixture_mib']} MiB text + one changing document",
        "",
        "| Run | Time | Added | Updated | Removed | Skipped | Failed |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for key, label in (
        ("initial", "Initial"),
        ("unchanged", "Unchanged"),
        ("single_file_update", "Single update"),
    ):
        item = benchmark[key]
        stats = item["stats"]
        lines.append(
            "| {label} | {seconds:.3f} s | {added} | {updated} | "
            "{removed} | {skipped} | {failed} |".format(
                label=label,
                seconds=item["seconds"],
                added=stats["added_count"],
                updated=stats["updated_count"],
                removed=stats["removed_count"],
                skipped=stats["skipped_count"],
                failed=stats["failed_count"],
            )
        )
    lines.extend(
        [
            "",
            f"- Diagnostic status: `{benchmark['diagnosis']['status']}`",
            f"- Index fingerprint checked: "
            f"{not any(issue['code'] == 'index_fingerprint_mismatch' for issue in benchmark['diagnosis']['issues'])}",
            "",
        ]
    )
    markdown_path.write_text("\n".join(lines), encoding="utf-8")
    return json_path, markdown_path


def main() -> int:
    args = parse_args()
    acceptance = run_acceptance_tests()
    benchmark = run_benchmark(args.fixture_mib)
    passed = acceptance["passed"] and benchmark["passed"]
    report = {
        "phase": "P0-7",
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "passed": passed,
        "acceptance": acceptance,
        "benchmark": benchmark,
    }
    json_path, markdown_path = write_report(
        report,
        args.output_dir.expanduser().resolve(),
    )
    print(f"P0-7 acceptance: {'PASS' if passed else 'FAIL'}")
    print(f"JSON report: {json_path}")
    print(f"Markdown report: {markdown_path}")
    return 0 if passed else 2


if __name__ == "__main__":
    raise SystemExit(main())
