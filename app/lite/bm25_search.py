from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import sqlite3
from typing import Any

from app.lite.indexer import (
    DEFAULT_INDEX_DIR,
    chunk_search_text,
    chunk_structure,
    ensure_index_format,
    read_chunks,
)
from app.retrieval_signals import (
    looks_like_summary_query,
    noise_penalty,
    query_intent_bonus,
)


BM25_INDEX_FILE = "bm25_index.sqlite3"
BM25_INDEX_VERSION = 3
MAX_QUERY_TOKENS = 96
TECHNICAL_TOKEN_PATTERN = re.compile(
    r"[a-z0-9]+(?:[._%/\-][a-z0-9]+)*%?",
    re.IGNORECASE,
)


def search_bm25_index(
    query: str,
    index_dir: str | Path = DEFAULT_INDEX_DIR,
    top_k: int = 5,
    *,
    source_paths: set[str] | None = None,
    node_types: set[str] | None = None,
) -> list[dict[str, Any]]:
    index_path = Path(index_dir).expanduser().resolve()
    ensure_index_format(index_path)
    chunks = read_chunks(index_path)
    if not chunks:
        raise FileNotFoundError(f"Lite index not found or empty: {index_path}")
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
    eligible_chunks = [
        chunk
        for chunk in chunks
        if _chunk_matches_filters(
            chunk,
            normalized_source_paths,
            normalized_node_types,
        )
    ]
    if not eligible_chunks:
        return []

    if looks_like_summary_query(query):
        return _summary_results(eligible_chunks, top_k)

    query_tokens = _query_tokens(query)
    if not query_tokens:
        return _first_results(eligible_chunks, top_k)

    database_path = _ensure_bm25_index(index_path, chunks)
    match_expression = " OR ".join(f'"{token}"' for token in query_tokens)
    candidate_limit = max(top_k * 4, 20)

    conditions = ["chunks_fts MATCH ?"]
    parameters: list[Any] = [match_expression]
    if normalized_source_paths:
        normalized_sources = sorted(normalized_source_paths)
        placeholders = ", ".join("?" for _ in normalized_sources)
        conditions.append(f"source_path IN ({placeholders})")
        parameters.extend(normalized_sources)
    if normalized_node_types:
        normalized_types = sorted(normalized_node_types)
        placeholders = ", ".join("?" for _ in normalized_types)
        conditions.append(f"node_type IN ({placeholders})")
        parameters.extend(normalized_types)
    parameters.append(candidate_limit)
    connection = sqlite3.connect(database_path)
    try:
        rows = connection.execute(
            f"""
            SELECT chunk_order, bm25(chunks_fts) AS bm25_score
            FROM chunks_fts
            WHERE {" AND ".join(conditions)}
            ORDER BY bm25_score
            LIMIT ?
            """,
            parameters,
        ).fetchall()
    finally:
        connection.close()

    ranked = []
    for chunk_order, raw_score in rows:
        record = chunks[int(chunk_order)]
        score = -float(raw_score)
        content = str(record.get("content") or "")
        score += query_intent_bonus(query, content)
        score -= noise_penalty(content)
        ranked.append((score, int(chunk_order)))
    ranked.sort(key=lambda item: item[0], reverse=True)

    if not ranked:
        return _first_results(eligible_chunks, top_k)

    results = []
    for rank, (score, chunk_order) in enumerate(ranked[: max(top_k, 1)], 1):
        results.append(_record_to_result(chunks[chunk_order], score, rank))
    return results


def bm25_tokens(text: str) -> list[str]:
    normalized = str(text or "").casefold()
    tokens = [_normalize_technical_token(match.group(0)) for match in TECHNICAL_TOKEN_PATTERN.finditer(normalized)]
    cjk_text = "".join(re.findall(r"[\u4e00-\u9fff]", normalized))
    for size in (4, 3, 2, 1):
        tokens.extend(
            cjk_text[index : index + size]
            for index in range(max(len(cjk_text) - size + 1, 0))
        )
    return [token for token in tokens if token]


def _query_tokens(text: str) -> list[str]:
    unique = []
    seen = set()
    for token in bm25_tokens(text):
        if token in seen:
            continue
        seen.add(token)
        unique.append(token)
        if len(unique) >= MAX_QUERY_TOKENS:
            break
    return unique


def _document_tokens(text: str) -> str:
    return " ".join(bm25_tokens(text))


def _ensure_bm25_index(index_path: Path, chunks: list[dict[str, Any]]) -> Path:
    database_path = index_path / BM25_INDEX_FILE
    fingerprint = _chunks_fingerprint(chunks)
    if _bm25_index_is_current(database_path, fingerprint, len(chunks)):
        return database_path

    index_path.mkdir(parents=True, exist_ok=True)
    temporary_path = database_path.with_suffix(database_path.suffix + ".tmp")
    if temporary_path.exists():
        temporary_path.unlink()

    connection = sqlite3.connect(temporary_path)
    try:
        connection.execute(
            """
            CREATE VIRTUAL TABLE chunks_fts USING fts5(
                chunk_order UNINDEXED,
                source_path UNINDEXED,
                node_type UNINDEXED,
                tokens,
                tokenize='unicode61'
            )
            """
        )
        connection.execute(
            "CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        connection.executemany(
            "INSERT INTO chunks_fts(chunk_order, source_path, node_type, tokens) "
            "VALUES (?, ?, ?, ?)",
            [
                (
                    index,
                    str(
                        chunk.get("source_path")
                        or chunk.get("filename")
                        or ""
                    ).casefold(),
                    str(chunk.get("node_type") or "").casefold(),
                    _document_tokens(chunk_search_text(chunk)),
                )
                for index, chunk in enumerate(chunks)
            ],
        )
        connection.executemany(
            "INSERT INTO metadata(key, value) VALUES (?, ?)",
            [
                ("version", str(BM25_INDEX_VERSION)),
                ("fingerprint", fingerprint),
                ("chunk_count", str(len(chunks))),
            ],
        )
        connection.commit()
    finally:
        connection.close()

    temporary_path.replace(database_path)
    return database_path


def _bm25_index_is_current(
    database_path: Path,
    fingerprint: str,
    chunk_count: int,
) -> bool:
    if not database_path.exists():
        return False
    connection = None
    try:
        connection = sqlite3.connect(database_path)
        metadata = dict(connection.execute("SELECT key, value FROM metadata"))
        indexed_count = int(
            connection.execute("SELECT count(*) FROM chunks_fts").fetchone()[0]
        )
        return (
            int(metadata.get("version", 0)) == BM25_INDEX_VERSION
            and metadata.get("fingerprint") == fingerprint
            and int(metadata.get("chunk_count", 0)) == chunk_count
            and indexed_count == chunk_count
        )
    except (sqlite3.Error, TypeError, ValueError):
        return False
    finally:
        if connection is not None:
            connection.close()


def _chunks_fingerprint(chunks: list[dict[str, Any]]) -> str:
    digest = hashlib.sha256()
    for chunk in chunks:
        digest.update(str(chunk.get("id") or "").encode("utf-8"))
        digest.update(b"\0")
        digest.update(chunk_search_text(chunk).encode("utf-8"))
        digest.update(b"\0")
    return digest.hexdigest()


def _normalize_technical_token(token: str) -> str:
    normalized = str(token or "").casefold()
    replacements = {
        ".": "dot",
        "%": "pct",
        "/": "slash",
        "-": "dash",
        "_": "under",
    }
    for source, target in replacements.items():
        normalized = normalized.replace(source, target)
    return f"tech{normalized}" if normalized else ""


def _chunk_matches_filters(
    chunk: dict[str, Any],
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


def _summary_results(chunks: list[dict[str, Any]], top_k: int) -> list[dict[str, Any]]:
    selected = []
    source_paths = set()
    for chunk in chunks:
        source_path = str(chunk.get("source_path") or chunk.get("filename") or "")
        if source_path in source_paths or int(chunk.get("chunk_index") or 0) != 0:
            continue
        source_paths.add(source_path)
        selected.append(chunk)
        if len(selected) >= top_k:
            break
    if len(selected) < top_k:
        selected_ids = {str(chunk.get("id") or "") for chunk in selected}
        for chunk in chunks:
            if str(chunk.get("id") or "") in selected_ids:
                continue
            selected.append(chunk)
            if len(selected) >= top_k:
                break
    return [
        _record_to_result(chunk, 0.0, rank)
        for rank, chunk in enumerate(selected, 1)
    ]


def _first_results(chunks: list[dict[str, Any]], top_k: int) -> list[dict[str, Any]]:
    return [
        _record_to_result(chunk, 0.0, rank)
        for rank, chunk in enumerate(chunks[: max(top_k, 1)], 1)
    ]


def _record_to_result(record: dict[str, Any], score: float, rank: int) -> dict[str, Any]:
    return {
        **chunk_structure(record),
        "rank": rank,
        "score": float(score),
        "source_path": record.get("source_path"),
        "filename": record.get("filename"),
        "chunk_index": record.get("chunk_index"),
        "content": record.get("content", ""),
        "content_chars": record.get("content_chars", 0),
    }
