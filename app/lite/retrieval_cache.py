from __future__ import annotations

from collections import OrderedDict
from copy import deepcopy
import hashlib
import json
from pathlib import Path
from threading import RLock
from typing import Any, Mapping

from app.lite.indexer import INDEX_MANIFEST_FILE, ensure_index_format


MAX_RETRIEVAL_CACHE_ENTRIES = 128
_CACHE: OrderedDict[str, list[dict[str, Any]]] = OrderedDict()
_LOCK = RLock()


def build_retrieval_cache_key(
    query: str,
    index_dir: str | Path,
    *,
    top_k: int,
    mode: str,
    models: Mapping[str, str],
    parameters: Mapping[str, Any],
) -> str:
    index_path = Path(index_dir).expanduser().resolve()
    payload = {
        "version": 1,
        "index_fingerprint": index_fingerprint(index_path),
        "knowledge_scope": index_path.as_posix(),
        "query": str(query or "").strip(),
        "top_k": int(top_k),
        "mode": str(mode),
        "models": dict(models),
        "parameters": dict(parameters),
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def index_fingerprint(index_dir: str | Path) -> str:
    index_path = Path(index_dir).expanduser().resolve()
    ensure_index_format(index_path)
    manifest_path = index_path / INDEX_MANIFEST_FILE
    if manifest_path.exists():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            fingerprint = str(manifest.get("index_fingerprint") or "")
            if fingerprint:
                return fingerprint
        except (OSError, TypeError, json.JSONDecodeError):
            pass

    chunks_path = index_path / "chunks.jsonl"
    digest = hashlib.sha256()
    with chunks_path.open("rb") as reader:
        while True:
            chunk = reader.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def get_cached_retrieval(key: str) -> list[dict[str, Any]] | None:
    with _LOCK:
        value = _CACHE.get(key)
        if value is None:
            return None
        _CACHE.move_to_end(key)
        return deepcopy(value)


def set_cached_retrieval(
    key: str,
    sources: list[dict[str, Any]],
) -> None:
    with _LOCK:
        _CACHE[key] = deepcopy(sources)
        _CACHE.move_to_end(key)
        while len(_CACHE) > MAX_RETRIEVAL_CACHE_ENTRIES:
            _CACHE.popitem(last=False)


def clear_retrieval_cache() -> None:
    with _LOCK:
        _CACHE.clear()
