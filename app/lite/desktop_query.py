from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from app.core.config import settings
from app.lite.bm25_search import search_bm25_index
from app.lite.generator import answer_query, extractive_answer
from app.lite.indexer import chunk_structure, list_index_documents, read_chunks
from app.lite.parent_context import ParentContextResolver
from app.lite.structured_query import run_structured_computation
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

_CLARIFY_HISTORY_MARKERS = (
    "请指定",
    "请说明",
    "请选择",
    "检测到文件内多个",
    "不明确",
    "无法确定",
    "请补充",
)
_QUESTION_WORDS = ("？", "?", "什么", "如何", "怎么", "为什么", "多少", "哪", "请", "分别", "几")


def _resolve_clarification_followup(
    query: str,
    conversation_history: list[dict[str, str]] | None,
) -> str:
    """用户回答上一轮澄清追问时（如输入"sheet1"），组合成完整查询。

    多级澄清场景：
    历史 = [用户:"sheet有几行", 助手:"…请指定哪个 Sheet…",
            用户:"sheet1", 助手:"…请指定哪个文件…"]，当前输入"动态因子"
    → 返回"sheet有几行 sheet1 动态因子"，让计算路由按指定 Sheet+文件重算。
    """
    query = (query or "").strip()
    if not query or not conversation_history:
        return query
    # 只处理简短、非疑问的澄清回应（如 sheet 名、文件名）
    if len(query) > 15 or any(word in query for word in _QUESTION_WORDS):
        return query
    # 历史最后一条 assistant 是否为澄清追问
    last_assistant = next(
        (
            message
            for message in reversed(conversation_history)
            if message.get("role") == "assistant"
        ),
        None,
    )
    if not last_assistant:
        return query
    clarify_text = str(last_assistant.get("content") or "")
    if not any(marker in clarify_text for marker in _CLARIFY_HISTORY_MARKERS):
        return query
    # 找到最近的原始完整问题（含疑问词或较长），并收集其后的所有澄清回应。
    original = ""
    followups: list[str] = []
    for message in conversation_history:
        if message.get("role") != "user":
            continue
        content = str(message.get("content") or "").strip()
        if not content:
            continue
        if any(word in content for word in _QUESTION_WORDS) or len(content) > 15:
            original = content
            followups = []
        elif original:
            followups.append(content)
    if not original:
        return query
    return " ".join([original] + followups + [query])


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
    conversation_history: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    # 完全离线模式：强制本地检索与本地答案，不发起任何远程请求。
    # 用户仍可获得基于本地索引的抽取式回答，而不是被拦截报错。
    if offline:
        use_llm = False
        use_embedding = False
        use_reranker = False
    # 澄清回应补全：用户回答上一轮"请指定哪个 Sheet"时（如输入"sheet1"），
    # 把当前简短输入和最近一次用户问题组合，避免被当作独立查询而"资料不足"。
    query = _resolve_clarification_followup(query, conversation_history)
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
            conversation_history=conversation_history,
        )
        text = str(answer["answer"] or "").strip()
        # 兜底：LLM 失败或返回空时，用结构化摘要保证"excel讲了什么"总有回答。
        if answer["mode"] in ("llm_error", "empty") or not text:
            fallback_text = extractive_answer(sources)
            return {
                "answer": fallback_text,
                "mode": "local_fallback",
                "sources": filter_sources_by_answer(
                    fallback_text,
                    sources,
                    "local_fallback",
                ),
                "retrieved_sources": sources,
                "llm": answer.get("llm"),
                "retrieval": {
                    "mode": "summary",
                    "services_used": _retrieval_services_used(
                        use_llm, False, False, bool(use_llm)
                    ),
                    "embedding": False,
                    "reranker": False,
                    "cache_hit": False,
                    "offline": offline,
                    "remote": bool(use_llm),
                    "query_plan": query_plan.cache_parameters(),
                },
            }
        return {
            "answer": text,
            "mode": answer["mode"],
            "sources": filter_sources_by_answer(
                text,
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
    if query_plan.is_computation:
        if _is_mixed_computation_query(query, documents):
            return await _mixed_computation_result(
                index_dir,
                query_plan,
                query,
                offline=offline,
                top_k=top_k,
                use_llm=use_llm,
                llm_api_key=llm_api_key,
                llm_base_url=llm_base_url,
                llm_model=llm_model,
            )
        return await _structured_computation_result(
            index_dir,
            query_plan,
            query,
            offline=offline,
            use_llm=use_llm,
            llm_api_key=llm_api_key,
            llm_base_url=llm_base_url,
            llm_model=llm_model,
        )
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
        conversation_history=conversation_history,
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
            "mode": retrieval_mode,
            "services_used": _retrieval_services_used(
                use_llm, use_embedding, use_reranker, remote_requested
            ),
            "embedding": use_embedding,
            "reranker": use_reranker,
            "cache_hit": cache_hit,
            "offline": offline,
            "remote": remote_requested,
            "query_plan": query_plan.cache_parameters(),
        },
    }


def _retrieval_services_used(
    use_llm: bool,
    use_embedding: bool,
    use_reranker: bool,
    remote_requested: bool,
) -> list[str]:
    services = []
    if use_llm and remote_requested:
        services.append("llm")
    if use_embedding:
        services.append("embedding")
    if use_reranker:
        services.append("reranker")
    return services


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
            "mode": "inventory",
            "services_used": [],
            "embedding": False,
            "reranker": False,
            "cache_hit": False,
            "offline": False,
            "remote": False,
            "query_plan": query_plan.cache_parameters(),
        },
    }


async def _structured_computation_result(
    index_dir: str | Path,
    query_plan: QueryPlan,
    query: str,
    *,
    offline: bool,
    use_llm: bool,
    llm_api_key: str,
    llm_base_url: str,
    llm_model: str,
) -> dict[str, Any]:
    """P1-2 结构化计算：纯 Python 白名单计算；LLM 兜底消歧可选。"""
    llm_available = bool(use_llm and llm_api_key)
    if llm_available:
        from app.lite.computation_llm import arun_computation_with_fallback

        result = await arun_computation_with_fallback(
            query,
            index_dir,
            source_paths=query_plan.source_paths,
            llm_api_key=llm_api_key,
            llm_base_url=llm_base_url,
            llm_model=llm_model,
        )
    else:
        result = run_structured_computation(
            query,
            index_dir,
            source_paths=query_plan.source_paths,
        )
    matched_rows = result.get("matched_rows") or []
    cap = settings.STRUCTURED_COMPUTATION_MAX_RESULT_ROWS
    sources = [
        _row_to_source(row, rank)
        for rank, row in enumerate(matched_rows[:cap], 1)
    ]
    return {
        "answer": result["answer"],
        "mode": result["mode"],
        "sources": sources,
        "retrieved_sources": sources,
        "llm": {"enabled": False, "usage": None},
        "retrieval": {
            "mode": "structured",
            "services_used": [],
            "embedding": False,
            "reranker": False,
            "cache_hit": False,
            "offline": offline,
            "remote": False,
            "query_plan": query_plan.cache_parameters(),
        },
    }


_MIXED_CONJUNCTIONS = ("和", "以及", "还有", "并且", "同时", "+")
_MIXED_CONTENT_MARKERS = (
    "怎么", "如何", "规定", "制度", "流程", "说明", "要求", "是什么", "情况",
)
_NON_TABULAR_SUFFIXES = (".md", ".pdf", ".txt")


def _is_mixed_computation_query(
    query: str,
    documents: list[dict[str, Any]],
) -> bool:
    """计算 + 文档检索混合：有连接词+内容词，或点名了非表格文件。"""
    normalized = str(query or "")
    has_non_tabular = any(
        str(document.get("filename") or "").lower().endswith(_NON_TABULAR_SUFFIXES)
        for document in documents
    )
    if not has_non_tabular:
        return False
    joined = any(marker in normalized for marker in _MIXED_CONJUNCTIONS) and any(
        marker in normalized for marker in _MIXED_CONTENT_MARKERS
    )
    names_doc = any(
        str(document.get("filename") or "") in normalized
        for document in documents
        if str(document.get("filename") or "").lower().endswith(_NON_TABULAR_SUFFIXES)
    )
    return joined or names_doc


async def _mixed_computation_result(
    index_dir: str | Path,
    query_plan: QueryPlan,
    query: str,
    *,
    offline: bool,
    top_k: int,
    use_llm: bool,
    llm_api_key: str,
    llm_base_url: str,
    llm_model: str,
) -> dict[str, Any]:
    """混合：表格部分走确定性计算，文档部分走 RAG，模板合并。"""
    comp = run_structured_computation(
        query,
        index_dir,
        source_paths=query_plan.source_paths,
    )
    cap = settings.STRUCTURED_COMPUTATION_MAX_RESULT_ROWS
    comp_sources = [
        _row_to_source(row, rank)
        for rank, row in enumerate((comp.get("matched_rows") or [])[:cap], 1)
    ]
    doc_sources = search_bm25_index(query, index_dir, top_k=top_k)
    llm_available = bool(use_llm and llm_api_key)
    if llm_available:
        from app.lite.computation_llm import synthesize_mixed

        combined = await synthesize_mixed(
            query,
            comp["answer"],
            comp_sources,
            doc_sources,
            api_key=llm_api_key,
            base_url=llm_base_url,
            model=llm_model,
        )
        doc_answer = {
            "answer": "",
            "mode": "llm",
            "llm": {"enabled": True, "usage": None},
        }
        if not combined:
            combined = f"【计算结果】{comp['answer']}"
    else:
        doc_answer = await answer_query(
            query,
            doc_sources,
            use_llm,
            api_key=llm_api_key,
            base_url=llm_base_url,
            model=llm_model,
        )
        combined = (
            f"【计算结果】{comp['answer']}\n\n"
            f"【相关资料】{doc_answer['answer']}"
        )
    return {
        "answer": combined,
        "mode": "mixed",
        "sources": comp_sources
        + filter_sources_by_answer(
            doc_answer.get("answer") or "",
            doc_sources,
            doc_answer.get("mode") or "",
        ),
        "retrieved_sources": comp_sources + doc_sources,
        "llm": doc_answer.get("llm"),
        "retrieval": {
            "mode": "mixed",
            "services_used": _retrieval_services_used(
                use_llm, False, False, bool(use_llm)
            ),
            "embedding": False,
            "reranker": False,
            "cache_hit": False,
            "offline": offline,
            "remote": bool(use_llm),
            "query_plan": query_plan.cache_parameters(),
        },
    }


def _row_to_source(row: dict[str, Any], rank: int) -> dict[str, Any]:
    cells = row.get("cells") or {}
    header = list(cells.keys())
    line = "\t".join(str(cells.get(column) or "") for column in header)
    content = line
    if header:
        content = "\t".join(header) + "\n" + line
    return {
        "rank": rank,
        "filename": str(row.get("filename") or ""),
        "source_path": str(row.get("source_path") or ""),
        "chunk_index": 0,
        "node_type": "row_group",
        "content": content,
        "row_numbers": [row.get("row_number")],
        "source_anchor": {
            "source_path": str(row.get("source_path") or ""),
            "sheet": str(row.get("sheet") or ""),
            "row_numbers": [row.get("row_number")],
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
