from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from app.core.config import settings
from app.lite.bm25_search import search_bm25_index
from app.lite.generator import answer_query
from app.lite.indexer import chunk_structure, list_index_documents, read_chunks
from app.lite.parent_context import ParentContextResolver
from app.lite.query_planner import QueryPlan, plan_query
from app.lite.remote_retrieval import (
    DEFAULT_EMBEDDING_MODEL,
    DEFAULT_RERANKER_MODEL,
    DEFAULT_RETRIEVAL_BASE_URL,
    RemoteModelError,
    rerank_sources,
    semantic_search_index,
)
from app.lite.retrieval_cache import (
    build_retrieval_cache_key,
    get_cached_retrieval,
    set_cached_retrieval,
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
    offline: bool = False,
    use_parent_context: bool = settings.PARENT_CONTEXT_ENABLED,
) -> dict[str, Any]:
    # 完全离线模式：强制本地检索与本地答案，不发起任何远程请求。
    # 用户仍可获得基于本地索引的抽取式回答，而不是被拦截报错。
    if offline:
        use_llm = False
        use_embedding = False
        use_reranker = False
    documents = list_index_documents(index_dir)
    query_plan = plan_query(query, documents)
    if query_plan.is_structured_inventory:
        return _inventory_result(query_plan, documents)
    if query_plan.is_summary:
        sources = _summary_sources(index_dir, query_plan, top_k)
        answer = await answer_query(
            query,
            sources,
            use_llm,
            api_key=llm_api_key,
            base_url=llm_base_url,
            model=llm_model,
            instructions=(
                "按文件名分别回答，每个已提供文件单独列出，"
                "优先说明 Sheet、字段和数据范围，不得省略文件。"
            ),
        )
        return {
            "answer": answer["answer"],
            "mode": answer["mode"],
            "sources": filter_sources_by_answer(
                answer["answer"],
                sources,
                answer["mode"],
            ),
            "retrieved_sources": sources,
            "llm": answer.get("llm"),
            "retrieval": {
                "embedding": False,
                "reranker": False,
                "cache_hit": False,
                "offline": offline,
                "remote": bool(use_llm),
                "query_plan": query_plan.cache_parameters(),
            },
        }
    remote_requested = use_llm or use_embedding or use_reranker

    candidate_k = (
        max(top_k * 3, 15)
        if query_plan.require_document_diversity
        else max(top_k * 2, 10)
        if use_reranker
        else top_k
    )
    source_paths = (
        set(query_plan.source_paths)
        if query_plan.source_paths
        else None
    )
    retrieval_mode = (
        "hybrid_rerank"
        if use_embedding and use_reranker
        else "hybrid"
        if use_embedding
        else "bm25_rerank"
        if use_reranker
        else "bm25"
    )
    cache_key = build_retrieval_cache_key(
        query,
        index_dir,
        top_k=top_k,
        mode=retrieval_mode,
        models={
            "embedding": embedding_model if use_embedding else "",
            "reranker": reranker_model if use_reranker else "",
        },
        parameters={
            "candidate_k": candidate_k,
            "query_plan": query_plan.cache_parameters(),
            "retrieval_base_url": (
                retrieval_base_url.rstrip("/")
                if use_embedding or use_reranker
                else ""
            ),
        },
    )
    sources = get_cached_retrieval(cache_key)
    cache_hit = sources is not None
    try:
        if sources is None:
            if use_embedding:
                semantic_sources = semantic_search_index(
                    query,
                    index_dir,
                    candidate_k,
                    api_key=retrieval_api_key,
                    base_url=retrieval_base_url,
                    model=embedding_model,
                    source_paths=source_paths,
                )
                lexical_sources = search_bm25_index(
                    query,
                    index_dir,
                    candidate_k,
                    source_paths=source_paths,
                )
                sources = _merge_hybrid_candidates(
                    semantic_sources,
                    lexical_sources,
                    limit=candidate_k,
                )
            else:
                sources = search_bm25_index(
                    query,
                    index_dir,
                    candidate_k,
                    source_paths=source_paths,
                )

            if use_reranker:
                sources = rerank_sources(
                    query,
                    sources,
                    (
                        min(candidate_k, len(sources))
                        if query_plan.require_document_diversity
                        else top_k
                    ),
                    api_key=retrieval_api_key,
                    base_url=retrieval_base_url,
                    model=reranker_model,
                )
            sources = _diversify_sources(
                sources,
                top_k=top_k,
                enabled=query_plan.require_document_diversity,
            )
            set_cached_retrieval(cache_key, sources)
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
                "cache_hit": cache_hit,
                "offline": offline,
                "remote": remote_requested,
                "error": str(exc),
                "query_plan": query_plan.cache_parameters(),
            },
        }

    # P1-1：内容类查询把命中的小块扩展到父章节/父表格，再送入生成。
    # 放在缓存读写之后，缓存仍存精简 child，父上下文每次查询现算。
    if use_parent_context and query_plan.requires_retrieval:
        ParentContextResolver(
            index_dir,
            max_parent_chars=settings.PARENT_CONTEXT_MAX_PARENT_CHARS,
            max_total_chars=settings.PARENT_CONTEXT_MAX_TOTAL_CHARS,
        ).resolve(sources)

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
                "cache_hit": cache_hit,
                "offline": offline,
                "remote": remote_requested,
                "query_plan": query_plan.cache_parameters(),
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
            "cache_hit": cache_hit,
            "offline": offline,
            "remote": remote_requested,
            "query_plan": query_plan.cache_parameters(),
        },
    }


def _inventory_result(
    query_plan: QueryPlan,
    documents: list[dict[str, Any]],
) -> dict[str, Any]:
    selected = _documents_for_plan(query_plan, documents)
    label = _inventory_label(query_plan.file_types)
    filenames = [str(document.get("filename") or "") for document in selected]
    if query_plan.intent == "inventory_count":
        answer = f"当前知识库有 {len(selected)} 个{label}文件"
        if filenames:
            answer += "：\n" + "\n".join(
                f"{index}. {filename}"
                for index, filename in enumerate(filenames, 1)
            )
        else:
            answer += "。"
    else:
        answer = (
            f"当前知识库中的{label}文件：\n"
            + "\n".join(
                f"{index}. {filename}"
                for index, filename in enumerate(filenames, 1)
            )
            if filenames
            else f"当前知识库没有{label}文件。"
        )
    sources = [
        {
            "rank": index,
            "filename": filename,
            "source_path": str(
                document.get("source_path") or filename
            ),
            "chunk_index": 0,
            "content": f"{label}文件：{filename}",
            "node_type": "document_inventory",
            "score": 1.0,
        }
        for index, (filename, document) in enumerate(
            zip(filenames, selected),
            1,
        )
    ]
    return {
        "answer": answer,
        "mode": "structured",
        "sources": sources,
        "retrieved_sources": sources,
        "llm": {"enabled": False, "usage": None},
        "retrieval": {
            "embedding": False,
            "reranker": False,
            "cache_hit": False,
            "offline": False,
            "remote": False,
            "query_plan": query_plan.cache_parameters(),
        },
    }


def _summary_sources(
    index_dir: str | Path,
    query_plan: QueryPlan,
    top_k: int,
) -> list[dict[str, Any]]:
    chunks = read_chunks(index_dir)
    requested = {
        str(source_path).casefold()
        for source_path in query_plan.source_paths
    }
    grouped: dict[str, list[dict[str, Any]]] = {}
    for chunk in chunks:
        source_path = str(
            chunk.get("source_path") or chunk.get("filename") or ""
        )
        if requested and source_path.casefold() not in requested:
            continue
        grouped.setdefault(source_path.casefold(), []).append(chunk)

    selected = []
    for source_path in query_plan.source_paths:
        candidates = grouped.get(str(source_path).casefold(), [])
        preferred = [
            chunk
            for chunk in candidates
            if chunk.get("node_type") == "workbook_summary"
        ]
        if not preferred:
            preferred = [
                chunk
                for chunk in candidates
                if chunk.get("node_type") == "sheet_summary"
            ]
        if not preferred and candidates:
            preferred = [candidates[0]]
        selected.extend(preferred[:3])

    limit = max(top_k, len(query_plan.source_paths))
    results = []
    for rank, chunk in enumerate(selected[:limit], 1):
        results.append(
            {
                **chunk_structure(chunk),
                "rank": rank,
                "score": 1.0,
                "source_path": chunk.get("source_path"),
                "filename": chunk.get("filename"),
                "chunk_index": chunk.get("chunk_index"),
                "content": chunk.get("content", ""),
                "content_chars": chunk.get("content_chars", 0),
            }
        )
    return results


def _documents_for_plan(
    query_plan: QueryPlan,
    documents: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    requested = {
        str(source_path).casefold()
        for source_path in query_plan.source_paths
    }
    if not requested:
        return list(documents)
    return [
        document
        for document in documents
        if str(
            document.get("source_path") or document.get("filename") or ""
        ).casefold()
        in requested
    ]


def _inventory_label(file_types: tuple[str, ...]) -> str:
    if file_types == (".xlsx",):
        return " Excel "
    if file_types == (".csv",):
        return " CSV "
    if file_types == (".pdf",):
        return " PDF "
    return ""


def _diversify_sources(
    sources: list[dict[str, Any]],
    *,
    top_k: int,
    enabled: bool,
) -> list[dict[str, Any]]:
    if not enabled:
        selected = list(sources[:top_k])
    else:
        per_document_limit = max(1, (top_k * 3 + 4) // 5)
        selected = []
        counts: dict[str, int] = {}
        selected_keys = set()
        for source in sources:
            source_path = str(
                source.get("source_path") or source.get("filename") or ""
            ).casefold()
            if counts.get(source_path, 0) >= per_document_limit:
                continue
            selected.append(source)
            selected_keys.add(_source_key(source))
            counts[source_path] = counts.get(source_path, 0) + 1
            if len(selected) >= top_k:
                break
        if len(selected) < top_k:
            for source in sources:
                if _source_key(source) in selected_keys:
                    continue
                selected.append(source)
                if len(selected) >= top_k:
                    break
    for rank, source in enumerate(selected, 1):
        source["rank"] = rank
    return selected


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
