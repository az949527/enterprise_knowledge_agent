from __future__ import annotations

import argparse
import ctypes
from datetime import datetime
import gc
from io import StringIO
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest


ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.documents import CSV_PARSER_VERSION, XLSX_PARSER_VERSION, iter_document_nodes


DEFAULT_OUTPUT = ROOT_DIR / "outputs" / "evals"
DEFAULT_CSV_MIB = 50
DEFAULT_XLSX_ROWS = 200_000
CSV_MAX_RSS_DELTA_MIB = 128.0
XLSX_MAX_RSS_DELTA_MIB = 256.0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run P0-6 CSV/XLSX acceptance and streaming memory benchmarks."
    )
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--csv-mib", type=int, default=DEFAULT_CSV_MIB)
    parser.add_argument("--xlsx-rows", type=int, default=DEFAULT_XLSX_ROWS)
    parser.add_argument("--skip-memory", action="store_true")
    parser.add_argument("--measure-path", type=Path, help=argparse.SUPPRESS)
    return parser.parse_args()


def run_acceptance_tests() -> dict:
    suite = unittest.defaultTestLoader.discover(
        str(ROOT_DIR / "tests"),
        pattern="test_p0_6_acceptance.py",
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


def create_large_csv(path: Path, target_mib: int) -> None:
    target_bytes = target_mib * 1024 * 1024
    row_number = 0
    with path.open("w", encoding="utf-8", newline="") as writer:
        writer.write("record_id,department,amount,description\n")
        while writer.tell() < target_bytes:
            batch = []
            for _ in range(5000):
                row_number += 1
                batch.append(
                    f"{row_number},department_{row_number % 17},"
                    f"{row_number % 100000},"
                    f"streaming acceptance row {row_number:09d}\n"
                )
            writer.write("".join(batch))


def create_large_xlsx(path: Path, row_count: int) -> None:
    from openpyxl import Workbook

    workbook = Workbook(write_only=True)
    sheet = workbook.create_sheet("large_sheet")
    sheet.append(["record_id", "department", "amount", "ratio", "event_date"])
    for row_number in range(1, row_count + 1):
        sheet.append(
            [
                row_number,
                f"department_{row_number % 17}",
                row_number % 100000,
                (row_number % 1000) / 1000,
                "2026-08-02",
            ]
        )
    workbook.save(path)
    workbook.close()


def current_rss_bytes() -> int:
    try:
        import psutil

        return int(psutil.Process(os.getpid()).memory_info().rss)
    except (ImportError, OSError):
        pass
    if sys.platform == "win32":
        return windows_rss_bytes()
    if sys.platform.startswith("linux"):
        try:
            fields = Path("/proc/self/statm").read_text(
                encoding="ascii"
            ).split()
            return int(fields[1]) * int(os.sysconf("SC_PAGE_SIZE"))
        except (OSError, IndexError, ValueError):
            return 0
    return 0


def windows_rss_bytes() -> int:
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
    process = kernel32.GetCurrentProcess()
    success = psapi.GetProcessMemoryInfo(
        process,
        ctypes.byref(counters),
        ctypes.sizeof(counters),
    )
    return int(counters.WorkingSetSize) if success else 0


def measure_document(path: Path) -> dict:
    gc.collect()
    baseline_rss = current_rss_bytes()
    if baseline_rss <= 0:
        raise RuntimeError("Process RSS measurement is unavailable.")
    peak_rss = baseline_rss
    stop_event = threading.Event()

    def sample_memory() -> None:
        nonlocal peak_rss
        while not stop_event.wait(0.01):
            peak_rss = max(peak_rss, current_rss_bytes())

    sampler = threading.Thread(target=sample_memory, daemon=True)
    sampler.start()
    started = time.perf_counter()
    node_count = 0
    content_chars = 0
    node_types: dict[str, int] = {}
    try:
        for node in iter_document_nodes(path):
            node_count += 1
            content_chars += len(node.content)
            node_types[node.node_type.value] = node_types.get(node.node_type.value, 0) + 1
    finally:
        stop_event.set()
        sampler.join()
    elapsed = time.perf_counter() - started
    peak_rss = max(peak_rss, current_rss_bytes())
    return {
        "path": str(path),
        "input_bytes": path.stat().st_size,
        "elapsed_seconds": elapsed,
        "baseline_rss_bytes": baseline_rss,
        "peak_rss_bytes": peak_rss,
        "rss_delta_bytes": max(peak_rss - baseline_rss, 0),
        "node_count": node_count,
        "content_chars": content_chars,
        "node_types": node_types,
    }


def run_measurement(path: Path) -> dict:
    completed = subprocess.run(
        [
            sys.executable,
            str(Path(__file__).resolve()),
            "--measure-path",
            str(path),
        ],
        cwd=ROOT_DIR,
        check=True,
        capture_output=True,
        text=True,
    )
    lines = [line for line in completed.stdout.splitlines() if line.strip()]
    if not lines:
        raise RuntimeError(f"Memory measurement produced no output for {path}.")
    return json.loads(lines[-1])


def mib(value: int) -> float:
    return value / (1024 * 1024)


def write_report(report: dict, output_dir: Path) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    json_path = output_dir / f"p0_6_eval_{timestamp}.json"
    markdown_path = output_dir / f"p0_6_eval_{timestamp}.md"
    json_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    lines = [
        "# P0-6 CSV/XLSX Acceptance",
        "",
        f"- Result: {'PASS' if report['passed'] else 'FAIL'}",
        f"- CSV parser: `{report['parser_versions']['csv']}`",
        f"- XLSX parser: `{report['parser_versions']['xlsx']}`",
        f"- Acceptance tests: {report['acceptance']['tests_run']}",
        "",
    ]
    if report["memory"]:
        lines.extend(
            [
                "| Fixture | Input | Nodes | Time | Peak RSS delta | Threshold | Result |",
                "|---|---:|---:|---:|---:|---:|---|",
            ]
        )
        for name in ("csv", "xlsx"):
            item = report["memory"][name]
            lines.append(
                "| {name} | {input_mib:.2f} MiB | {nodes} | {seconds:.2f} s | "
                "{delta_mib:.2f} MiB | {threshold:.2f} MiB | {result} |".format(
                    name=name.upper(),
                    input_mib=mib(item["input_bytes"]),
                    nodes=item["node_count"],
                    seconds=item["elapsed_seconds"],
                    delta_mib=mib(item["rss_delta_bytes"]),
                    threshold=item["threshold_mib"],
                    result="PASS" if item["passed"] else "FAIL",
                )
            )
        lines.append("")
    markdown_path.write_text("\n".join(lines), encoding="utf-8")
    return json_path, markdown_path


def main() -> int:
    args = parse_args()
    if args.measure_path is not None:
        print(json.dumps(measure_document(args.measure_path.resolve()), ensure_ascii=False))
        return 0

    acceptance = run_acceptance_tests()
    memory: dict[str, dict] = {}
    if not args.skip_memory:
        with tempfile.TemporaryDirectory(prefix="p0_6_eval_") as temp_dir:
            root = Path(temp_dir)
            csv_path = root / "large.csv"
            xlsx_path = root / "large.xlsx"
            create_large_csv(csv_path, args.csv_mib)
            create_large_xlsx(xlsx_path, args.xlsx_rows)
            csv_result = run_measurement(csv_path)
            xlsx_result = run_measurement(xlsx_path)
            csv_result["threshold_mib"] = CSV_MAX_RSS_DELTA_MIB
            xlsx_result["threshold_mib"] = XLSX_MAX_RSS_DELTA_MIB
            csv_result["passed"] = (
                mib(csv_result["rss_delta_bytes"]) <= CSV_MAX_RSS_DELTA_MIB
            )
            xlsx_result["passed"] = (
                mib(xlsx_result["rss_delta_bytes"]) <= XLSX_MAX_RSS_DELTA_MIB
            )
            memory = {"csv": csv_result, "xlsx": xlsx_result}

    passed = acceptance["passed"] and all(
        result["passed"] for result in memory.values()
    )
    report = {
        "phase": "P0-6",
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "passed": passed,
        "parser_versions": {
            "csv": CSV_PARSER_VERSION,
            "xlsx": XLSX_PARSER_VERSION,
        },
        "acceptance": acceptance,
        "memory": memory,
    }
    json_path, markdown_path = write_report(report, args.output_dir.resolve())
    print(f"P0-6 acceptance: {'PASS' if passed else 'FAIL'}")
    print(f"JSON report: {json_path}")
    print(f"Markdown report: {markdown_path}")
    return 0 if passed else 2


if __name__ == "__main__":
    raise SystemExit(main())
