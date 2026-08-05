from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Any

from app.lite import indexer


BM25_DATABASE = "bm25_index.sqlite3"
EMBEDDING_MANIFEST = "embeddings_manifest.json"
EMBEDDING_VECTORS = "embeddings.f32"


def diagnose_index(
    index_dir: str | Path = indexer.DEFAULT_INDEX_DIR,
) -> dict[str, Any]:
    index_path = Path(index_dir).expanduser().resolve()
    result: dict[str, Any] = {
        "index_dir": index_path.as_posix(),
        "status": "missing",
        "ready": False,
        "recovered_transaction": False,
        "issues": [],
        "warnings": [],
        "counts": {
            "documents": 0,
            "nodes": 0,
            "parents": 0,
            "chunks": 0,
        },
        "cache": {
            "bm25": "missing",
            "embedding": "missing",
        },
    }
    if not index_path.exists():
        _issue(result, "index_missing", "索引目录不存在。")
        return result

    try:
        result["recovered_transaction"] = indexer.recover_index_transaction(
            index_path
        )
    except indexer.IndexFormatError as exc:
        _issue(result, "transaction_corrupt", str(exc))
        result["status"] = "corrupt"
        return result

    manifest_path = index_path / indexer.INDEX_MANIFEST_FILE
    if not manifest_path.exists():
        _issue(result, "manifest_missing", "索引清单不存在，需要重建索引。")
        return result

    try:
        indexer.ensure_index_format(index_path)
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (indexer.IndexFormatError, OSError, json.JSONDecodeError) as exc:
        _issue(result, "manifest_invalid", str(exc))
        result["status"] = "corrupt"
        return result

    documents = manifest.get("documents")
    if isinstance(documents, list):
        result["counts"]["documents"] = len(documents)
    else:
        _issue(result, "documents_invalid", "索引清单 documents 字段无效。")

    node_ids: set[str] = set()
    document_ids: set[str] = set()
    try:
        for node in indexer.iter_nodes(index_path):
            result["counts"]["nodes"] += 1
            node_ids.add(node.node_id)
            document_ids.add(node.document_id)
    except indexer.IndexFormatError as exc:
        _issue(result, "nodes_invalid", str(exc))

    parent_ids: set[str] = set()
    try:
        for parent in indexer.iter_parents(index_path):
            result["counts"]["parents"] += 1
            parent_id = str(parent.get("parent_id") or "")
            content_node_id = str(parent.get("content_node_id") or "")
            if not parent_id:
                _issue(result, "parent_id_missing", "父节点记录缺少 parent_id。")
            else:
                parent_ids.add(parent_id)
            if content_node_id not in node_ids:
                _issue(
                    result,
                    "parent_node_missing",
                    f"父节点 {parent_id or '<unknown>'} 引用了不存在的节点。",
                )
    except indexer.IndexFormatError as exc:
        _issue(result, "parents_invalid", str(exc))

    chunk_fingerprint = hashlib.sha256()
    try:
        for chunk in indexer.iter_chunks(index_path):
            result["counts"]["chunks"] += 1
            node_id = str(chunk.get("node_id") or "")
            document_id = str(chunk.get("document_id") or "")
            parent_id = str(chunk.get("parent_id") or "")
            chunk_fingerprint.update(
                str(chunk.get("id") or "").encode("utf-8")
            )
            chunk_fingerprint.update(b"\0")
            chunk_fingerprint.update(
                indexer.chunk_fingerprint_text(chunk).encode("utf-8")
            )
            chunk_fingerprint.update(b"\0")
            if node_id not in node_ids:
                _issue(
                    result,
                    "chunk_node_missing",
                    f"Chunk {chunk.get('id') or '<unknown>'} 引用了不存在的节点。",
                )
            if document_id not in document_ids:
                _issue(
                    result,
                    "chunk_document_missing",
                    f"Chunk {chunk.get('id') or '<unknown>'} 的文档引用无效。",
                )
            if parent_id and parent_id not in parent_ids:
                _issue(
                    result,
                    "chunk_parent_missing",
                    f"Chunk {chunk.get('id') or '<unknown>'} 的父节点引用无效。",
                )
    except indexer.IndexFormatError as exc:
        _issue(result, "chunks_invalid", str(exc))

    expected_chunks = int(manifest.get("chunk_count") or 0)
    expected_documents = int(manifest.get("file_count") or 0)
    if expected_chunks != result["counts"]["chunks"]:
        _issue(
            result,
            "chunk_count_mismatch",
            f"manifest 记录 {expected_chunks} 个 Chunk，实际为 "
            f"{result['counts']['chunks']}。",
        )
    if expected_documents != result["counts"]["documents"]:
        _issue(
            result,
            "document_count_mismatch",
            f"manifest 记录 {expected_documents} 个文档，实际为 "
            f"{result['counts']['documents']}。",
        )
    expected_fingerprint = str(manifest.get("index_fingerprint") or "")
    if (
        expected_fingerprint
        and expected_fingerprint != chunk_fingerprint.hexdigest()
    ):
        _issue(
            result,
            "index_fingerprint_mismatch",
            "索引指纹与 Chunk 内容不一致。",
        )

    _diagnose_bm25(index_path, result)
    _diagnose_embedding_cache(index_path, result)
    _diagnose_residual_files(index_path, result)

    if result["issues"]:
        result["status"] = "corrupt"
    elif result["warnings"] and result["counts"]["chunks"] > 0:
        result["status"] = "degraded"
        result["ready"] = True
    elif result["counts"]["chunks"] > 0:
        result["status"] = "healthy"
        result["ready"] = True
    else:
        result["status"] = "empty"
    return result


def _diagnose_bm25(index_path: Path, result: dict[str, Any]) -> None:
    database_path = index_path / BM25_DATABASE
    if not database_path.exists():
        return
    connection = None
    try:
        connection = sqlite3.connect(database_path)
        integrity = str(connection.execute("PRAGMA quick_check").fetchone()[0])
        if integrity != "ok":
            raise sqlite3.DatabaseError(integrity)
        metadata = dict(connection.execute("SELECT key, value FROM metadata"))
        indexed_count = int(
            connection.execute("SELECT count(*) FROM chunks_fts").fetchone()[0]
        )
        if indexed_count != result["counts"]["chunks"]:
            _warning(
                result,
                "bm25_stale",
                "BM25 缓存与当前 Chunk 数量不一致，下一次查询将重建。",
            )
            result["cache"]["bm25"] = "stale"
        elif int(metadata.get("chunk_count", 0)) != indexed_count:
            _warning(
                result,
                "bm25_metadata_stale",
                "BM25 缓存元数据不一致，下一次查询将重建。",
            )
            result["cache"]["bm25"] = "stale"
        else:
            result["cache"]["bm25"] = "healthy"
    except (sqlite3.Error, TypeError, ValueError) as exc:
        _warning(
            result,
            "bm25_corrupt",
            f"BM25 缓存损坏，下一次查询将重建：{exc}",
        )
        result["cache"]["bm25"] = "corrupt"
    finally:
        if connection is not None:
            connection.close()


def _diagnose_embedding_cache(
    index_path: Path,
    result: dict[str, Any],
) -> None:
    manifest_path = index_path / EMBEDDING_MANIFEST
    vectors_path = index_path / EMBEDDING_VECTORS
    if not manifest_path.exists() and not vectors_path.exists():
        return
    if not manifest_path.exists() or not vectors_path.exists():
        _warning(
            result,
            "embedding_cache_incomplete",
            "Embedding 缓存文件不完整，下次远程检索将重建。",
        )
        result["cache"]["embedding"] = "corrupt"
        return
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        dimension = int(manifest.get("dimension") or 0)
        chunk_count = int(manifest.get("chunk_count") or 0)
        cache_version = int(manifest.get("version") or 0)
        chunk_keys = list(manifest.get("chunk_keys") or [])
        expected_bytes = dimension * chunk_count * 4
        if (
            cache_version != 2
            or dimension <= 0
            or len(chunk_keys) != chunk_count
            or vectors_path.stat().st_size != expected_bytes
        ):
            raise ValueError("vector file size does not match manifest")
        index_manifest = json.loads(
            (index_path / indexer.INDEX_MANIFEST_FILE).read_text(
                encoding="utf-8"
            )
        )
        if (
            chunk_count != result["counts"]["chunks"]
            or manifest.get("fingerprint")
            != index_manifest.get("index_fingerprint")
        ):
            _warning(
                result,
                "embedding_cache_stale",
                "Embedding 缓存与当前索引不一致，下次远程检索将重建。",
            )
            result["cache"]["embedding"] = "stale"
        else:
            result["cache"]["embedding"] = "healthy"
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        _warning(
            result,
            "embedding_cache_corrupt",
            f"Embedding 缓存损坏，下次远程检索将重建：{exc}",
        )
        result["cache"]["embedding"] = "corrupt"


def _diagnose_residual_files(
    index_path: Path,
    result: dict[str, Any],
) -> None:
    residual = sorted(
        {
            path.name
            for pattern in ("*.tmp", "*.bak", ".*.bak")
            for path in index_path.glob(pattern)
            if path.is_file()
        }
    )
    if residual:
        _warning(
            result,
            "residual_files",
            "发现残留临时文件：" + "、".join(residual),
        )


def _issue(result: dict[str, Any], code: str, message: str) -> None:
    result["issues"].append({"code": code, "message": message})


def _warning(result: dict[str, Any], code: str, message: str) -> None:
    result["warnings"].append({"code": code, "message": message})
