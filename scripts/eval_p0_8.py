from __future__ import annotations

import argparse
from datetime import datetime
from io import StringIO
import json
from pathlib import Path
import sys
import unittest


ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.lite.remote_retrieval import (
    RemoteModelError,
    remote_access_enabled,
    set_remote_access,
)
from app.security import (
    DEFAULT_SERVICE,
    delete_secret,
    get_backend_name,
    get_secret,
    redact_secrets,
    set_secret,
)


DEFAULT_OUTPUT = ROOT_DIR / "outputs" / "evals"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run P0-8 security and offline boundary acceptance."
    )
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    return parser.parse_args()


def run_acceptance_tests() -> dict:
    suite = unittest.defaultTestLoader.discover(
        str(ROOT_DIR / "tests"),
        pattern="test_p0_8_acceptance.py",
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


def run_security_checks() -> dict:
    checks: dict = {}

    # 1. 完全离线门禁
    set_remote_access(False)
    blocked = None
    try:
        from app.lite.remote_retrieval import RemoteModelConfig, embed_texts

        embed_texts(
            ["文本"],
            RemoteModelConfig("key", "https://example.test/v1", "model"),
            mode="embedding_error",
        )
    except RemoteModelError as exc:
        blocked = {"mode": exc.mode, "message": redact_secrets(str(exc))}
    set_remote_access(True)
    checks["offline_gate"] = {
        "passed": (
            blocked is not None
            and blocked["mode"] == "embedding_error"
            and remote_access_enabled()
        ),
        "blocked": blocked,
        "restored_online": remote_access_enabled(),
    }

    # 2. 系统凭据 round-trip
    account = "p0_8_eval_account"
    set_secret(DEFAULT_SERVICE, account, "roundtrip-value")
    stored = get_secret(DEFAULT_SERVICE, account)
    delete_secret(DEFAULT_SERVICE, account)
    cleared = get_secret(DEFAULT_SERVICE, account)
    checks["credential_round_trip"] = {
        "passed": stored == "roundtrip-value" and cleared == "",
        "backend": get_backend_name(),
        "stored": stored == "roundtrip-value",
        "cleared": cleared == "",
    }

    # 3. API Key 脱敏
    samples = [
        "Authorization: Bearer sk-abcdef1234567890",
        "api_key=supersecretvalue123",
        "普通错误信息",
    ]
    redacted = [redact_secrets(sample) for sample in samples]
    checks["redaction"] = {
        "passed": (
            "sk-abcdef1234567890" not in redacted[0]
            and "supersecretvalue123" not in redacted[1]
        ),
        "samples": [
            {"input": sample, "output": output}
            for sample, output in zip(samples, redacted)
        ],
    }
    return checks


def write_report(report: dict, output_dir: Path) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    json_path = output_dir / f"p0_8_eval_{timestamp}.json"
    markdown_path = output_dir / f"p0_8_eval_{timestamp}.md"
    json_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    security = report["security"]
    lines = [
        "# P0-8 Security And Offline Boundary Acceptance",
        "",
        f"- Result: {'PASS' if report['passed'] else 'FAIL'}",
        f"- Acceptance tests: {report['acceptance']['tests_run']}",
        f"- Credential backend: `{security['credential_round_trip']['backend']}`",
        f"- Offline gate blocks remote: "
        f"`{security['offline_gate']['blocked'] is not None}`",
        f"- Credential round-trip: "
        f"`{security['credential_round_trip']['stored']}` / cleared "
        f"`{security['credential_round_trip']['cleared']}`",
        "",
        "| 敏感模式 | 脱敏后 |",
        "|---|---|",
    ]
    for item in security["redaction"]["samples"]:
        lines.append(
            f"| `{item['input'][:44]}` | `{item['output'][:44]}` |"
        )
    lines.append("")
    markdown_path.write_text("\n".join(lines), encoding="utf-8")
    return json_path, markdown_path


def main() -> int:
    args = parse_args()
    acceptance = run_acceptance_tests()
    security = run_security_checks()
    passed = acceptance["passed"] and all(
        security[key]["passed"]
        for key in ("offline_gate", "credential_round_trip", "redaction")
    )
    report = {
        "phase": "P0-8",
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "passed": passed,
        "acceptance": acceptance,
        "security": security,
    }
    json_path, markdown_path = write_report(
        report,
        args.output_dir.expanduser().resolve(),
    )
    print(f"P0-8 acceptance: {'PASS' if passed else 'FAIL'}")
    print(f"JSON report: {json_path}")
    print(f"Markdown report: {markdown_path}")
    return 0 if passed else 2


if __name__ == "__main__":
    raise SystemExit(main())
