from __future__ import annotations

import argparse
import json
import re
from pathlib import Path


SECRET_PATTERN = re.compile(rb"sk-[A-Za-z0-9_-]{12,}")


def read_known_filenames(root: Path) -> list[str]:
    manifest_path = root / "data" / "lite_index" / "manifest.json"
    if not manifest_path.exists():
        return []
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    return [
        str(document.get("filename") or "")
        for document in manifest.get("documents", [])
        if document.get("filename")
    ]


def configured_api_keys() -> list[str]:
    try:
        from PySide6.QtCore import QSettings

        keys = []
        for app_name in (
            "Local Knowledge Tool",
            "Local Knowledge Tool Desktop",
            "Local Knowledge Tool Desktop 1.0",
        ):
            settings = QSettings("EnterpriseKnowledgeAgent", app_name)
            settings.setFallbacksEnabled(False)
            value = str(settings.value("llm/api_key", "") or "")
            if value and value not in keys:
                keys.append(value)
        return keys
    except Exception:
        return []


def iter_release_files(release_dir: Path):
    for path in release_dir.rglob("*"):
        if path.is_file():
            yield path


def verify_release(root: Path, release_dir: Path) -> None:
    if not release_dir.exists():
        raise SystemExit(f"Release directory does not exist: {release_dir}")

    config_text = (root / "app" / "core" / "config.py").read_text(encoding="utf-8")
    if not re.search(r'LLM_API_KEY:\s*str\s*=\s*""', config_text):
        raise SystemExit("Release blocked: app/core/config.py contains a non-empty default LLM API key.")

    env_files = list(root.glob(".env")) + list(root.glob(".env.*"))
    if env_files:
        names = ", ".join(path.name for path in env_files)
        raise SystemExit(f"Release blocked: environment files exist in the project root: {names}")

    forbidden_names = {"chunks.jsonl", "manifest.json", "faiss_index.bin", "enterprise_knowledge_agent.db"}
    known_filenames = read_known_filenames(root)
    api_keys = configured_api_keys()
    api_key_bytes = []
    for api_key in api_keys:
        api_key_bytes.extend((api_key.encode("utf-8"), api_key.encode("utf-16-le")))

    for path in iter_release_files(release_dir):
        if path.name in forbidden_names or "data/lite_index" in path.as_posix():
            raise SystemExit(f"Release blocked: user data file included: {path}")

        content = path.read_bytes()
        if SECRET_PATTERN.search(content):
            raise SystemExit(f"Release blocked: possible API key found in: {path}")
        if any(secret and secret in content for secret in api_key_bytes):
            raise SystemExit(f"Release blocked: configured desktop API key found in: {path}")
        for filename in known_filenames:
            if filename.encode("utf-8") in content or filename.encode("utf-16-le") in content:
                raise SystemExit(f"Release blocked: indexed document filename found in: {path}")

    print(f"Release verification passed: {release_dir}")
    print(f"Checked {len(known_filenames)} historical document filename(s).")
    print("No index files, uploaded document names, .env files, or API keys were found.")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("release_dir")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    verify_release(root, Path(args.release_dir).resolve())


if __name__ == "__main__":
    main()
