from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from app.lite.bm25_search import search_bm25_index
from app.lite.generator import answer_query
from app.lite.remote_retrieval import (
    DEFAULT_EMBEDDING_MODEL,
    DEFAULT_RERANKER_MODEL,
    DEFAULT_RETRIEVAL_BASE_URL,
    RemoteModelError,
    rerank_sources,
    semantic_search_index,
)


async def query_desktop_index(
    query: str,
    index_dir: str | Path,
    *,
    top_k: int = 5,
    use_llm: bool,
    llm_api_key: str,
    llm_base_url: str,
    llm_model: str,
    use_embedding: bool,
    use_reranker: bool,
    retrieval_api_key: str,
    retrieval_base_url: str = DEFAULT_RETRIEVAL_BASE_URL,
    embedding_model: str = DEFAULT_EMBEDDING_MODEL,
    reranker_model: str = DEFAULT_RERANKER_MODEL,
) -> dict[str, Any]:
    candidate_k = max(top_k * 2, 10) if use_reranker else top_k
    try:
        if use_embedding:
            semantic_sources = semantic_search_index(
                query,
                index_dir,
                candidate_k,
                api_key=retrieval_api_key,
                base_url=retrieval_base_url,
                model=embedding_model,
            )
            lexical_sources = search_bm25_index(query, index_dir, candidate_k)
            sources = _merge_hybrid_candidates(
                semantic_sources,
                lexical_sources,
                limit=candidate_k,
            )
        else:
            sources = search_bm25_index(query, index_dir, candidate_k)

        if use_reranker:
            sources = rerank_sources(
                query,
                sources,
                top_k,
                api_key=retrieval_api_key,
                base_url=retrieval_base_url,
                model=reranker_model,
            )
        else:
            sources = sources[:top_k]
            for rank, source in enumerate(sources, 1):
                source["rank"] = rank
    except RemoteModelError as exc:
        return {
            "answer": str(exc),
            "mode": exc.mode,
            "sources": [],
            "retrieved_sources": [],
            "llm": {"enabled": False, "usage": None},
            "retrieval": {
                "embedding": use_embedding,
                "reranker": use_reranker,
                "error": str(exc),
            },
        }

    answer = await answer_query(
        query,
        sources,
        use_llm,
        api_key=llm_api_key,
        base_url=llm_base_url,
        model=llm_model,
    )
    if answer["mode"] == "llm_error":
        return {
            "answer": answer["answer"],
            "mode": answer["mode"],
            "sources": [],
            "retrieved_sources": [],
            "llm": answer.get("llm"),
            "retrieval": {
                "embedding": use_embedding,
                "reranker": use_reranker,
            },
        }

    return {
        "answer": answer["answer"],
        "mode": answer["mode"],
        "sources": filter_sources_by_answer(answer["answer"], sources, answer["mode"]),
        "retrieved_sources": sources,
        "llm": answer.get("llm"),
        "retrieval": {
            "embedding": use_embedding,
            "reranker": use_reranker,
        },
    }


def _merge_hybrid_candidates(
    semantic_sources: list[dict[str, Any]],
    lexical_sources: list[dict[str, Any]],
    *,
    limit: int,
) -> list[dict[str, Any]]:
    """Merge semantic and lexical rankings before optional remote reranking."""
    merged: dict[tuple[str, int, str], dict[str, Any]] = {}
    semantic_ranked = {key: rank for rank, (key, _) in enumerate(
        ((_source_key(source), source) for source in semantic_sources),
        1,
    )}
    lexical_ranked = {key: rank for rank, (key, _) in enumerate(
        ((_source_key(source), source) for source in lexical_sources),
        1,
    )}

    for source in semantic_sources + lexical_sources:
        key = _source_key(source)
        merged.setdefault(key, dict(source))

    ranked = []
    for key, source in merged.items():
        semantic_rank = semantic_ranked.get(key)
        lexical_rank = lexical_ranked.get(key)
        fusion_score = 0.0
        if semantic_rank is not None:
            fusion_score += 1.0 / (10 + semantic_rank)
        if lexical_rank is not None:
            fusion_score += 1.0 / (10 + lexical_rank)
        source["score"] = fusion_score
        ranked.append((fusion_score, source))

    ranked.sort(key=lambda item: item[0], reverse=True)
    results = []
    for rank, (_, source) in enumerate(ranked[: max(limit, 1)], 1):
        source["rank"] = rank
        results.append(source)
    return results


def _source_key(source: dict[str, Any]) -> tuple[str, int, str]:
    return (
        str(source.get("source_path") or ""),
        int(source.get("chunk_index") or 0),
        str(source.get("content") or ""),
    )


def filter_sources_by_answer(answer: str, sources: list[dict], mode: str = "") -> list[dict]:
    if mode.endswith("_error"):
        return []
    cited_ranks = [int(value) for value in re.findall(r"\[(\d+)\]", answer or "")]
    valid_ranks = []
    for rank in cited_ranks:
        if 1 <= rank <= len(sources) and rank not in valid_ranks:
            valid_ranks.append(rank)
    if mode == "llm" and not valid_ranks and "资料不足" in (answer or ""):
        return []
    if not valid_ranks:
        return sources
    return [sources[rank - 1] for rank in valid_ranks]
