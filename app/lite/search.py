from __future__ import annotations

import heapq
import json
import math
import re
from pathlib import Path
from typing import Any

from app.lite.indexer import DEFAULT_INDEX_DIR, chunk_structure, ensure_index_format
from app.retrieval_signals import (
    looks_like_summary_query,
    noise_penalty,
    query_intent_bonus,
)


def search_index(query: str, index_dir: str | Path = DEFAULT_INDEX_DIR, top_k: int = 5) -> list[dict[str, Any]]:
    chunks_path = Path(index_dir).expanduser().resolve() / "chunks.jsonl"
    if not chunks_path.exists():
        raise FileNotFoundError(f"Lite index not found: {chunks_path}")
    ensure_index_format(index_dir)

    query_terms = lexical_terms(query)
    if not query_terms:
        return []

    heap: list[tuple[float, int, dict[str, Any]]] = []
    first_chunks: list[dict[str, Any]] = []
    summary_chunks: list[dict[str, Any]] = []
    summary_sources: set[str] = set()
    with chunks_path.open("r", encoding="utf-8") as reader:
        for order, line in enumerate(reader):
            record = json.loads(line)
            if len(first_chunks) < top_k:
                first_chunks.append(_record_to_result(record, 0.0))
            source_key = str(record.get("source_path") or record.get("filename") or "")
            if (
                len(summary_chunks) < top_k
                and source_key not in summary_sources
                and int(record.get("chunk_index") or 0) == 0
            ):
                summary_chunks.append(_record_to_result(record, 0.0))
                summary_sources.add(source_key)
            score = lexical_score(query_terms, record.get("content", ""))
            score += query_intent_bonus(query, record.get("content", ""))
            score -= noise_penalty(record.get("content", ""))
            if score <= 0:
                continue
            item = _record_to_result(record, score)
            if len(heap) < top_k:
                heapq.heappush(heap, (score, order, item))
            else:
                heapq.heappushpop(heap, (score, order, item))

    results = [item for _, _, item in sorted(heap, key=lambda row: row[0], reverse=True)]
    if looks_like_summary_query(query):
        if len(summary_chunks) < top_k:
            existing_ids = {
                (item.get("source_path"), item.get("chunk_index"))
                for item in summary_chunks
            }
            for item in first_chunks:
                item_id = (item.get("source_path"), item.get("chunk_index"))
                if item_id in existing_ids:
                    continue
                summary_chunks.append(item)
                existing_ids.add(item_id)
                if len(summary_chunks) >= top_k:
                    break
        results = summary_chunks[:top_k]
    elif not results:
        results = first_chunks
    for rank, item in enumerate(results, 1):
        item["rank"] = rank
    return results


def _record_to_result(record: dict[str, Any], score: float) -> dict[str, Any]:
    return {
        **chunk_structure(record),
        "rank": 0,
        "score": score,
        "source_path": record.get("source_path"),
        "filename": record.get("filename"),
        "chunk_index": record.get("chunk_index"),
        "content": record.get("content", ""),
        "content_chars": record.get("content_chars", 0),
    }


def lexical_score(query_terms: set[str], text: str) -> float:
    text_terms = lexical_terms(text)
    if not text_terms:
        return 0.0
    overlap = query_terms & text_terms
    recall = len(overlap) / len(query_terms)
    precision = len(overlap) / max(math.sqrt(len(text_terms)), 1.0)
    return recall * 0.75 + precision * 0.25
def lexical_terms(text: str) -> set[str]:
    normalized = str(text).lower()
    latin_terms = set(re.findall(r"[a-z0-9]{2,}", normalized))
    cjk_text = "".join(re.findall(r"[\u4e00-\u9fff]", normalized))
    cjk_terms = {char for char in cjk_text}
    for size in (2, 3, 4):
        cjk_terms.update(cjk_text[index : index + size] for index in range(max(len(cjk_text) - size + 1, 0)))
    return latin_terms | cjk_terms
