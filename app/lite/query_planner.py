from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath
import re
from typing import Any, Iterable

from app.core.config import settings
from app.lite.structured_query import (
    AVG_MARKERS,
    COUNT_MARKERS as COMPUTATION_COUNT_MARKERS,
    CROSS_FILE_MARKERS,
    FILTER_MARKERS,
    MAX_MARKERS,
    MIN_MARKERS,
    SUM_MARKERS,
)


CONTENT_INTENT = "content"
INVENTORY_COUNT_INTENT = "inventory_count"
INVENTORY_LIST_INTENT = "inventory_list"
DOCUMENT_SUMMARY_INTENT = "document_summary"
MULTI_DOCUMENT_SUMMARY_INTENT = "multi_document_summary"
STRUCTURED_COMPUTATION_INTENT = "structured_computation"

_TABULAR_SUFFIXES = {".xlsx", ".csv"}
# 强算子：明确的计算动词，出现即倾向于计算
_STRONG_COMPUTATION_MARKERS = (
    SUM_MARKERS + AVG_MARKERS + COMPUTATION_COUNT_MARKERS
)
# 弱算子：最高/最低也可能是文档里的措辞，需表范围限定
_WEAK_COMPUTATION_MARKERS = MAX_MARKERS + MIN_MARKERS

COUNT_MARKERS = ("几个", "多少个", "几份", "数量", "how many")
LIST_MARKERS = ("有哪些文件", "有什么文件", "文件列表", "列出", "都有哪些", "有哪些")
SUMMARY_MARKERS = (
    "有什么",
    "包含什么",
    "有哪些内容",
    "主要内容",
    "什么内容",
    "主要讲了什么",
    "讲了什么",
    "讲什么",
    "说了什么",
    "哪些列",
    "字段",
    "列名",
    "概览",
    "总结",
    "摘要",
    "分别",
    "各自",
)
ALL_MARKERS = ("所有", "全部", "分别", "各自", "我的")
TABLE_SCOPE_MARKERS = ("表里", "表中", "表格里", "表格中", "excel表")
FILE_MARKERS = ("文件", "文档", "资料", "知识库")


@dataclass(frozen=True)
class QueryPlan:
    intent: str = CONTENT_INTENT
    source_paths: tuple[str, ...] = ()
    file_types: tuple[str, ...] = ()
    require_document_diversity: bool = False

    @property
    def is_structured_inventory(self) -> bool:
        return self.intent in {
            INVENTORY_COUNT_INTENT,
            INVENTORY_LIST_INTENT,
        }

    @property
    def is_summary(self) -> bool:
        return self.intent in {
            DOCUMENT_SUMMARY_INTENT,
            MULTI_DOCUMENT_SUMMARY_INTENT,
        }

    @property
    def is_computation(self) -> bool:
        return self.intent == STRUCTURED_COMPUTATION_INTENT

    @property
    def requires_retrieval(self) -> bool:
        return self.intent == CONTENT_INTENT

    def cache_parameters(self) -> dict[str, Any]:
        return {
            "intent": self.intent,
            "source_paths": list(self.source_paths),
            "file_types": list(self.file_types),
            "require_document_diversity": self.require_document_diversity,
        }


def plan_query(
    query: str,
    documents: Iterable[dict[str, Any]],
) -> QueryPlan:
    document_list = [dict(document) for document in documents]
    normalized_query = _normalize(query)
    requested_types = _requested_file_types(normalized_query)
    matching_documents = _matching_documents(normalized_query, document_list)
    summary_requested = any(marker in normalized_query for marker in SUMMARY_MARKERS)
    count_requested = any(marker in normalized_query for marker in COUNT_MARKERS)
    list_requested = any(marker in normalized_query for marker in LIST_MARKERS)
    has_file_scope = bool(
        requested_types
        or matching_documents
        or any(marker in normalized_query for marker in FILE_MARKERS)
    )

    if count_requested and has_file_scope:
        return QueryPlan(
            intent=INVENTORY_COUNT_INTENT,
            source_paths=_document_sources(
                _filter_documents(document_list, requested_types)
            ),
            file_types=tuple(sorted(requested_types)),
        )
    if list_requested and has_file_scope:
        return QueryPlan(
            intent=INVENTORY_LIST_INTENT,
            source_paths=_document_sources(
                _filter_documents(document_list, requested_types)
            ),
            file_types=tuple(sorted(requested_types)),
        )
    # P1-2 结构化计算：强算子只要有表格就计算；弱算子需表范围限定。
    if (
        settings.STRUCTURED_COMPUTATION_ENABLED
        and _has_any_tabular(document_list)
        and (
            _is_strong_computation_query(normalized_query)
            or (
                _is_weak_computation_query(normalized_query)
                and _has_table_scope(
                    normalized_query, document_list, requested_types, matching_documents
                )
            )
        )
    ):
        matching_tabular = [
            document
            for document in matching_documents
            if _document_suffix(document) in _TABULAR_SUFFIXES
        ]
        return QueryPlan(
            intent=STRUCTURED_COMPUTATION_INTENT,
            source_paths=(
                _document_sources(matching_tabular) if matching_tabular else ()
            ),
            file_types=tuple(sorted(requested_types & _TABULAR_SUFFIXES)),
        )
    if matching_documents:
        return QueryPlan(
            intent=(
                DOCUMENT_SUMMARY_INTENT
                if summary_requested
                else CONTENT_INTENT
            ),
            source_paths=_document_sources(matching_documents),
            file_types=tuple(
                sorted(
                    {
                        _document_suffix(document)
                        for document in matching_documents
                        if _document_suffix(document)
                    }
                )
            ),
        )

    table_summary = summary_requested and any(
        marker in normalized_query for marker in TABLE_SCOPE_MARKERS
    )
    typed_summary = summary_requested and bool(requested_types)
    broad_summary = summary_requested and any(
        marker in normalized_query for marker in ALL_MARKERS
    )
    if table_summary and not requested_types:
        requested_types = {".xlsx"}
    if typed_summary or table_summary or broad_summary:
        selected = _filter_documents(document_list, requested_types)
        if selected:
            return QueryPlan(
                intent=MULTI_DOCUMENT_SUMMARY_INTENT,
                source_paths=_document_sources(selected),
                file_types=tuple(sorted(requested_types)),
                require_document_diversity=True,
            )

    selected = _filter_documents(document_list, requested_types)
    return QueryPlan(
        intent=CONTENT_INTENT,
        source_paths=(
            _document_sources(selected)
            if requested_types
            else ()
        ),
        file_types=tuple(sorted(requested_types)),
        require_document_diversity=bool(
            any(marker in normalized_query for marker in ALL_MARKERS)
        ),
    )


def _matching_documents(
    normalized_query: str,
    documents: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    compact_query = _compact(normalized_query)
    matches = []
    for document in documents:
        filename = str(document.get("filename") or "")
        source_path = str(document.get("source_path") or filename)
        candidates = {
            filename.casefold(),
            PurePosixPath(filename).stem.casefold(),
            source_path.casefold(),
            PurePosixPath(source_path).stem.casefold(),
        }
        if any(
            candidate
            and len(_compact(candidate)) >= 2
            and (
                candidate in normalized_query
                or _compact(candidate) in compact_query
            )
            for candidate in candidates
        ):
            matches.append(document)
    return matches


def _requested_file_types(normalized_query: str) -> set[str]:
    requested = set()
    if any(
        marker in normalized_query
        for marker in ("excel", "xlsx", "工作簿", "电子表格")
    ):
        requested.add(".xlsx")
    if "csv" in normalized_query:
        requested.add(".csv")
    if "pdf" in normalized_query:
        requested.add(".pdf")
    if "markdown" in normalized_query or "md文件" in normalized_query:
        requested.add(".md")
    if "txt" in normalized_query or "文本文件" in normalized_query:
        requested.add(".txt")
    return requested


def _filter_documents(
    documents: list[dict[str, Any]],
    file_types: set[str],
) -> list[dict[str, Any]]:
    if not file_types:
        return list(documents)
    return [
        document
        for document in documents
        if _document_suffix(document) in file_types
    ]


def _document_sources(
    documents: Iterable[dict[str, Any]],
) -> tuple[str, ...]:
    return tuple(
        str(document.get("source_path") or document.get("filename") or "")
        for document in documents
        if document.get("source_path") or document.get("filename")
    )


def _document_suffix(document: dict[str, Any]) -> str:
    value = str(
        document.get("filename") or document.get("source_path") or ""
    )
    return PurePosixPath(value).suffix.casefold()


def _normalize(value: str) -> str:
    return str(value or "").strip().casefold()


def _compact(value: str) -> str:
    return re.sub(r"[\s._\-\\/：:（）()\[\]【】]+", "", value.casefold())


def _has_any_tabular(document_list: list[dict[str, Any]]) -> bool:
    return any(
        _document_suffix(document) in _TABULAR_SUFFIXES
        for document in document_list
    )


def _is_strong_computation_query(query: str) -> bool:
    if any(marker in query for marker in _STRONG_COMPUTATION_MARKERS):
        return True
    if any(marker in query for marker in FILTER_MARKERS) and re.search(r"\d", query):
        return True
    return False


def _is_weak_computation_query(query: str) -> bool:
    return any(marker in query for marker in _WEAK_COMPUTATION_MARKERS)


def _has_table_scope(
    query: str,
    document_list: list[dict[str, Any]],
    requested_types: set[str],
    matching_documents: list[dict[str, Any]],
) -> bool:
    tabular = [
        document
        for document in document_list
        if _document_suffix(document) in _TABULAR_SUFFIXES
    ]
    if not tabular:
        return False
    non_tabular = [
        document
        for document in document_list
        if _document_suffix(document) not in _TABULAR_SUFFIXES
    ]
    # 索引里只有表格文档：计算是唯一合理解读，直接放行。
    if not non_tabular:
        return True
    if requested_types & _TABULAR_SUFFIXES:
        return True
    if any(
        _document_suffix(document) in _TABULAR_SUFFIXES
        for document in matching_documents
    ):
        return True
    return any(marker in query for marker in TABLE_SCOPE_MARKERS)
