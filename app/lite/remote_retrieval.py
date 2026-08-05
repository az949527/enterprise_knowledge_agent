from __future__ import annotations

from array import array
from collections import defaultdict, deque
from dataclasses import dataclass
import hashlib
import heapq
import json
import math
from pathlib import Path
import sys
from typing import Any

import httpx

from app.lite.indexer import (
    DEFAULT_INDEX_DIR,
    chunk_search_text,
    chunk_structure,
    ensure_index_format,
    read_chunks,
)
from app.security.redaction import redact_secrets
from app.security.remote_access import remote_access_enabled, set_remote_access


DEFAULT_RETRIEVAL_BASE_URL = "https://api.siliconflow.cn/v1"
DEFAULT_EMBEDDING_MODEL = "BAAI/bge-m3"
DEFAULT_RERANKER_MODEL = "BAAI/bge-reranker-v2-m3"
EMBEDDING_CACHE_FILE = "embeddings.f32"
EMBEDDING_CACHE_MANIFEST = "embeddings_manifest.json"
EMBEDDING_CACHE_VERSION = 2


class RemoteModelError(RuntimeError):
    def __init__(self, mode: str, message: str) -> None:
        super().__init__(message)
        self.mode = mode


def _assert_remote_access(mode: str) -> None:
    if not remote_access_enabled():
        raise RemoteModelError(
            mode,
            "当前处于完全离线模式，已禁止远程调用。请关闭离线模式或仅使用本地检索。",
        )


@dataclass(frozen=True)
class RemoteModelConfig:
    api_key: str
    base_url: str
    model: str


def semantic_search_index(
    query: str,
    index_dir: str | Path = DEFAULT_INDEX_DIR,
    top_k: int = 10,
    *,
    api_key: str,
    base_url: str = DEFAULT_RETRIEVAL_BASE_URL,
    model: str = DEFAULT_EMBEDDING_MODEL,
    source_paths: set[str] | None = None,
    node_types: set[str] | None = None,
) -> list[dict[str, Any]]:
    _assert_remote_access("embedding_error")
    config = _validated_config("embedding_error", "Embedding", api_key, base_url, model)
    index_path = Path(index_dir).expanduser().resolve()
    ensure_index_format(index_path)
    chunks = read_chunks(index_path)
    if not chunks:
        raise FileNotFoundError(f"Lite index not found or empty: {index_path}")

    vectors, dimension = _load_or_create_embedding_cache(index_path, chunks, config)
    query_vectors = embed_texts([query], config, mode="embedding_error")
    query_vector = query_vectors[0]
    if len(query_vector) != dimension:
        raise RemoteModelError(
            "embedding_error",
            f"Embedding 返回维度与现有索引不一致：查询 {len(query_vector)}，索引 {dimension}。请检查模型设置。",
        )

    query_norm = _vector_norm(query_vector)
    normalized_source_paths = (
        {str(value).casefold() for value in source_paths}
        if source_paths
        else None
    )
    normalized_node_types = (
        {str(value).casefold() for value in node_types}
        if node_types
        else None
    )
    scored: list[tuple[float, int]] = []
    for index in range(len(chunks)):
        if not _chunk_matches_filters(
            chunks[index],
            source_paths=normalized_source_paths,
            node_types=normalized_node_types,
        ):
            continue
        start = index * dimension
        vector = vectors[start : start + dimension]
        score = _cosine_similarity(query_vector, query_norm, vector)
        scored.append((score, index))

    best = heapq.nlargest(max(top_k, 1), scored, key=lambda item: item[0])
    results = []
    for rank, (score, index) in enumerate(best, 1):
        record = chunks[index]
        results.append(
            {
                **chunk_structure(record),
                "rank": rank,
                "score": float(score),
                "source_path": record.get("source_path"),
                "filename": record.get("filename"),
                "chunk_index": record.get("chunk_index"),
                "content": record.get("content", ""),
                "content_chars": record.get("content_chars", 0),
            }
        )
    return results


def rerank_sources(
    query: str,
    candidates: list[dict[str, Any]],
    top_n: int,
    *,
    api_key: str,
    base_url: str = DEFAULT_RETRIEVAL_BASE_URL,
    model: str = DEFAULT_RERANKER_MODEL,
) -> list[dict[str, Any]]:
    if not candidates:
        return []

    _assert_remote_access("reranker_error")
    config = _validated_config("reranker_error", "Reranker", api_key, base_url, model)
    payload = _post_json(
        config,
        "/rerank",
        {
            "model": config.model,
            "query": query,
            "documents": [chunk_search_text(candidate) for candidate in candidates],
            "top_n": min(max(top_n, 1), len(candidates)),
            "return_documents": False,
        },
        mode="reranker_error",
        label="Reranker",
    )
    remote_results = payload.get("results")
    if not isinstance(remote_results, list) or not remote_results:
        raise RemoteModelError("reranker_error", "Reranker 请求成功，但服务没有返回排序结果。")

    ranked = []
    for rank, item in enumerate(remote_results, 1):
        try:
            index = int(item["index"])
            score = float(item["relevance_score"])
            source = dict(candidates[index])
        except (KeyError, TypeError, ValueError, IndexError) as exc:
            raise RemoteModelError("reranker_error", "Reranker 返回了无法识别的结果格式。") from exc
        source["rank"] = rank
        source["rerank_score"] = score
        ranked.append(source)
    return ranked


def embed_texts(
    texts: list[str],
    config: RemoteModelConfig,
    *,
    mode: str,
    batch_size: int = 32,
) -> list[list[float]]:
    if not texts:
        return []

    _assert_remote_access(mode)
    vectors: list[list[float]] = []
    with httpx.Client(timeout=60.0) as client:
        for start in range(0, len(texts), batch_size):
            batch = texts[start : start + batch_size]
            payload = _post_json(
                config,
                "/embeddings",
                {
                    "model": config.model,
                    "input": batch,
                    "encoding_format": "float",
                },
                mode=mode,
                label="Embedding",
                client=client,
            )
            data = payload.get("data")
            if not isinstance(data, list) or len(data) != len(batch):
                raise RemoteModelError(mode, "Embedding 请求成功，但返回的向量数量不正确。")
            try:
                ordered = sorted(data, key=lambda item: int(item["index"]))
                batch_vectors = [
                    [float(value) for value in item["embedding"]]
                    for item in ordered
                ]
            except (KeyError, TypeError, ValueError) as exc:
                raise RemoteModelError(mode, "Embedding 返回了无法识别的向量格式。") from exc
            vectors.extend(batch_vectors)

    dimensions = {len(vector) for vector in vectors}
    if not vectors or len(dimensions) != 1 or 0 in dimensions:
        raise RemoteModelError(mode, "Embedding 返回的向量维度无效或不一致。")
    return vectors


def _load_or_create_embedding_cache(
    index_path: Path,
    chunks: list[dict[str, Any]],
    config: RemoteModelConfig,
) -> tuple[array, int]:
    fingerprint = _chunks_fingerprint(chunks)
    chunk_keys = _chunk_cache_keys(chunks)
    cached = _load_embedding_cache_state(index_path, config)
    if cached is not None:
        cached_vectors, dimension, cached_keys, cached_fingerprint = cached
        if cached_fingerprint == fingerprint and cached_keys == chunk_keys:
            return cached_vectors, dimension
    else:
        cached_vectors = array("f")
        cached_keys = []
        dimension = 0

    cached_positions: dict[str, deque[int]] = defaultdict(deque)
    for index, key in enumerate(cached_keys):
        cached_positions[key].append(index)
    reused_positions: dict[int, int] = {}
    missing_positions: list[int] = []
    missing_texts: list[str] = []
    for index, (chunk, key) in enumerate(zip(chunks, chunk_keys)):
        if cached_positions[key]:
            reused_positions[index] = cached_positions[key].popleft()
        else:
            missing_positions.append(index)
            missing_texts.append(chunk_search_text(chunk))

    new_vectors = (
        embed_texts(missing_texts, config, mode="embedding_error")
        if missing_texts
        else []
    )
    if new_vectors:
        new_dimension = len(new_vectors[0])
        if dimension and new_dimension != dimension:
            raise RemoteModelError(
                "embedding_error",
                "Embedding 模型维度发生变化，请删除旧缓存后重试。",
            )
        dimension = dimension or new_dimension
        if any(len(vector) != dimension for vector in new_vectors):
            raise RemoteModelError(
                "embedding_error",
                "Embedding 返回的文档向量维度不一致。",
            )
    if dimension <= 0:
        raise RemoteModelError("embedding_error", "Embedding 返回了空向量。")

    new_by_position = dict(zip(missing_positions, new_vectors))
    flattened = array("f")
    for index in range(len(chunks)):
        cached_index = reused_positions.get(index)
        if cached_index is not None:
            start = cached_index * dimension
            flattened.extend(cached_vectors[start : start + dimension])
        else:
            flattened.extend(new_by_position[index])
    _write_embedding_cache(
        index_path,
        flattened,
        dimension,
        len(chunks),
        fingerprint,
        chunk_keys,
        config,
    )
    return flattened, dimension


def _load_embedding_cache(
    index_path: Path,
    fingerprint: str,
    chunk_count: int,
    config: RemoteModelConfig,
) -> tuple[array, int] | None:
    cached = _load_embedding_cache_state(index_path, config)
    if cached is None:
        return None
    flattened, dimension, chunk_keys, cached_fingerprint = cached
    if cached_fingerprint != fingerprint or len(chunk_keys) != chunk_count:
        return None
    return flattened, dimension


def _load_embedding_cache_state(
    index_path: Path,
    config: RemoteModelConfig,
) -> tuple[array, int, list[str], str] | None:
    manifest_path = index_path / EMBEDDING_CACHE_MANIFEST
    vectors_path = index_path / EMBEDDING_CACHE_FILE
    if not manifest_path.exists() or not vectors_path.exists():
        return None

    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        dimension = int(manifest["dimension"])
        chunk_count = int(manifest.get("chunk_count") or 0)
        chunk_keys = [
            str(value)
            for value in (manifest.get("chunk_keys") or [])
        ]
        if (
            int(manifest.get("version") or 0) != EMBEDDING_CACHE_VERSION
            or manifest.get("model") != config.model
            or manifest.get("base_url") != _normalized_base_url(config.base_url)
            or dimension <= 0
            or len(chunk_keys) != chunk_count
        ):
            return None

        flattened = array("f")
        flattened.frombytes(vectors_path.read_bytes())
        if sys.byteorder != "little":
            flattened.byteswap()
        if len(flattened) != chunk_count * dimension:
            return None
        return (
            flattened,
            dimension,
            chunk_keys,
            str(manifest.get("fingerprint") or ""),
        )
    except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
        return None


def _write_embedding_cache(
    index_path: Path,
    flattened: array,
    dimension: int,
    chunk_count: int,
    fingerprint: str,
    chunk_keys: list[str],
    config: RemoteModelConfig,
) -> None:
    index_path.mkdir(parents=True, exist_ok=True)
    vectors_path = index_path / EMBEDDING_CACHE_FILE
    manifest_path = index_path / EMBEDDING_CACHE_MANIFEST
    vectors_temp = vectors_path.with_suffix(vectors_path.suffix + ".tmp")
    manifest_temp = manifest_path.with_suffix(manifest_path.suffix + ".tmp")

    output = array("f", flattened)
    if sys.byteorder != "little":
        output.byteswap()
    vectors_temp.write_bytes(output.tobytes())
    manifest_temp.write_text(
        json.dumps(
            {
                "version": EMBEDDING_CACHE_VERSION,
                "model": config.model,
                "base_url": _normalized_base_url(config.base_url),
                "dimension": dimension,
                "chunk_count": chunk_count,
                "fingerprint": fingerprint,
                "chunk_keys": chunk_keys,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    vectors_temp.replace(vectors_path)
    manifest_temp.replace(manifest_path)


def _post_json(
    config: RemoteModelConfig,
    path: str,
    body: dict[str, Any],
    *,
    mode: str,
    label: str,
    client: httpx.Client | None = None,
) -> dict[str, Any]:
    _assert_remote_access(mode)
    endpoint = _api_endpoint(config.base_url, path)
    try:
        if client is None:
            with httpx.Client(timeout=60.0) as request_client:
                response = request_client.post(
                    endpoint,
                    headers={
                        "Authorization": f"Bearer {config.api_key}",
                        "Content-Type": "application/json",
                    },
                    json=body,
                )
        else:
            response = client.post(
                endpoint,
                headers={
                    "Authorization": f"Bearer {config.api_key}",
                    "Content-Type": "application/json",
                },
                json=body,
            )
    except httpx.TimeoutException as exc:
        raise RemoteModelError(mode, f"{label} 请求超时，请检查网络或稍后重试。") from exc
    except httpx.HTTPError as exc:
        raise RemoteModelError(
            mode,
            redact_secrets(f"{label} 请求失败，请检查网络和 Base URL。{exc}"),
        ) from exc

    if response.status_code >= 400:
        detail = redact_secrets(_response_error_detail(response))
        raise RemoteModelError(mode, f"{label} 请求失败（HTTP {response.status_code}）：{detail}")
    try:
        payload = response.json()
    except ValueError as exc:
        raise RemoteModelError(mode, f"{label} 服务返回了非 JSON 数据。") from exc
    if not isinstance(payload, dict):
        raise RemoteModelError(mode, f"{label} 服务返回了无法识别的数据格式。")
    return payload


def _validated_config(
    mode: str,
    label: str,
    api_key: str,
    base_url: str,
    model: str,
) -> RemoteModelConfig:
    resolved_key = str(api_key or "").strip()
    resolved_base_url = str(base_url or "").strip()
    resolved_model = str(model or "").strip()
    if not resolved_key:
        raise RemoteModelError(mode, f"未配置远程检索 API Key，无法使用 {label}。")
    if not resolved_base_url:
        raise RemoteModelError(mode, f"未配置远程检索 Base URL，无法使用 {label}。")
    if not resolved_model:
        raise RemoteModelError(mode, f"未配置 {label} 模型名称。")
    return RemoteModelConfig(resolved_key, resolved_base_url, resolved_model)


def _api_endpoint(base_url: str, path: str) -> str:
    normalized = _normalized_base_url(base_url)
    if normalized.endswith("/v1"):
        return normalized + path
    return normalized + "/v1" + path


def _normalized_base_url(base_url: str) -> str:
    return str(base_url or "").strip().rstrip("/")


def _chunks_fingerprint(chunks: list[dict[str, Any]]) -> str:
    digest = hashlib.sha256()
    for chunk in chunks:
        digest.update(str(chunk.get("id") or "").encode("utf-8"))
        digest.update(b"\0")
        digest.update(chunk_search_text(chunk).encode("utf-8"))
        digest.update(b"\0")
    return digest.hexdigest()


def _chunk_cache_keys(chunks: list[dict[str, Any]]) -> list[str]:
    keys = []
    for chunk in chunks:
        digest = hashlib.sha256()
        digest.update(str(chunk.get("id") or "").encode("utf-8"))
        digest.update(b"\0")
        digest.update(
            str(
                chunk.get("search_text")
                or chunk_search_text(chunk)
            ).encode("utf-8")
        )
        keys.append(digest.hexdigest())
    return keys


def _chunk_matches_filters(
    chunk: dict[str, Any],
    *,
    source_paths: set[str] | None,
    node_types: set[str] | None,
) -> bool:
    if source_paths:
        source_path = str(
            chunk.get("source_path") or chunk.get("filename") or ""
        ).casefold()
        if source_path not in source_paths:
            return False
    if node_types:
        node_type = str(chunk.get("node_type") or "").casefold()
        if node_type not in node_types:
            return False
    return True


def _vector_norm(vector) -> float:
    return math.sqrt(sum(float(value) * float(value) for value in vector))


def _cosine_similarity(query_vector: list[float], query_norm: float, document_vector) -> float:
    document_norm = _vector_norm(document_vector)
    if not query_norm or not document_norm:
        return 0.0
    dot = sum(float(left) * float(right) for left, right in zip(query_vector, document_vector))
    return dot / (query_norm * document_norm)


def _response_error_detail(response: httpx.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        text = response.text.strip()
        return text[:240] if text else "服务未返回错误详情"

    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict):
            detail = error.get("message") or error.get("detail")
            if detail:
                return str(detail)[:240]
        for key in ("message", "detail"):
            if payload.get(key):
                return str(payload[key])[:240]
    return "服务未返回错误详情"
