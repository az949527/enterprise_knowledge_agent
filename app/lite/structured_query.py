"""P1-2: Excel/CSV 结构化计算 —— 纯 Python 白名单计算，无 SQL，无 LLM 执行。

对命中的表格(单文件或跨文件)做确定性聚合/筛选/分组计算，返回带行列证据的结果。
安全边界：
- 只允许枚举算子(count/sum/avg/max/min/filter/argmax)
- 列名必须命中白名单（来自 sheet_summary 的 metadata.columns）
- 纯只读：从索引节点重建行，不改任何数据
- 字段/条件/目标 Sheet 不明时返回澄清，不猜
LLM 永不参与执行；远程 LLM 兜底（函数调用）只在列映射/混合拆分时作规划层。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Optional

from app.core.config import settings
from app.lite.indexer import read_chunks, read_nodes

# ---------- 标记词 ----------

SUM_MARKERS = ("求和", "合计", "总和", "加起来", "总额", "总计", "一共")
AVG_MARKERS = ("平均", "均值", "平均值", "人均")
MAX_MARKERS = ("最大", "最高", "最多", "峰值")
MIN_MARKERS = ("最小", "最低", "最少", "谷值")
COUNT_MARKERS = ("有几条", "多少行", "多少条", "几条", "几行", "条数", "计数")
FILTER_MARKERS = (
    "超过", "大于", "高于", "达到", "不低于", "少于", "小于", "低于", "不足",
)
GROUP_MARKERS = ("哪个", "哪些", "每", "各", "分", "按")
CROSS_FILE_MARKERS = ("所有", "全部", "每个", "各文件", "各表")

_TABULAR_SUFFIXES = {".xlsx", ".csv"}
_COMPARISON = {
    "超过": "gt",
    "大于": "gt",
    "高于": "gt",
    "达到": "ge",
    "不低于": "ge",
    "少于": "lt",
    "小于": "lt",
    "低于": "lt",
    "不足": "lt",
}
_CONDITION_RE = re.compile(
    r"(超过|大于|高于|达到|不低于|少于|小于|低于|不足|>=|<=|>|<)\s*([\d.,]+%?)"
)


@dataclass(frozen=True)
class ComputationSpec:
    operator: str = "count"            # count|sum|avg|max|min|filter
    column: Optional[str] = None       # 目标列展示名
    comparison: Optional[str] = None   # gt|ge|lt|le
    threshold: Optional[float] = None
    group_by: Optional[str] = None     # argmax/argmin 分组列
    aggregate: Optional[str] = None    # argmax 时的 max/min
    sheet: Optional[str] = None
    columns: tuple[str, ...] = ()      # 字段白名单
    source_paths: tuple[str, ...] = ()
    cross_file: bool = False           # 是否跨文件聚合
    clarify: Optional[str] = None      # 非空 => 答案是澄清
    matched_rows: tuple[dict[str, Any], ...] = ()


def run_structured_computation(
    query: str,
    index_dir: str | Path,
    source_paths: Iterable[str] = (),
    column_override: Optional[str] = None,
) -> dict[str, Any]:
    """对外入口：读索引 → 解析 spec → 重建行 → 计算 → 返回结果。

    column_override 供 LLM 兜底用：直接指定目标列（须在白名单内）。
    """
    index_path = Path(index_dir).expanduser().resolve()
    chunks = read_chunks(index_path)
    nodes = {node.node_id: node for node in read_nodes(index_path)}
    source_paths = tuple(
        str(value) for value in source_paths if value
    )
    spec = extract_computation_spec(
        query, chunks, nodes, source_paths, column_override=column_override
    )
    if spec.clarify:
        return {
            "answer": spec.clarify,
            "mode": "structured_clarify",
            "spec": spec,
            "matched_rows": [],
        }
    rows = resolve_rows(chunks, nodes, spec)
    spec = replace(spec, matched_rows=tuple(rows))
    # 聚合算子要求目标列存在数值，否则澄清。
    if spec.operator in ("sum", "avg", "max", "min") and spec.column:
        if not any(
            _numeric(_cell(row, spec.column)) is not None for row in rows
        ):
            return {
                "answer": (
                    f"列“{spec.column}”没有数值，无法计算。"
                    f"可用列：{_join_cols(spec.columns)}"
                ),
                "mode": "structured_clarify",
                "spec": spec,
                "matched_rows": [],
            }
    answer, matched_rows = compute_rows(rows, spec)
    spec = replace(spec, matched_rows=tuple(matched_rows))
    return {
        "answer": answer,
        "mode": "structured",
        "spec": spec,
        "matched_rows": matched_rows,
    }


def extract_computation_spec(
    query: str,
    chunks: list[dict[str, Any]],
    nodes: dict[str, Any],
    source_paths: tuple[str, ...] = (),
    column_override: Optional[str] = None,
) -> ComputationSpec:
    normalized = str(query or "").strip()
    if not normalized:
        return _clarify("问题为空，请说明要对哪个表格做什么计算。")

    # 目标文件：查询点名 → 点名文件；否则全部表格文档。
    target_paths = _target_source_paths(normalized, chunks, source_paths)
    if not target_paths:
        return _clarify("没有找到可计算的 Excel/CSV 表格文档。")

    sheets = _sheet_summaries(chunks, target_paths)
    if not sheets:
        return _clarify("目标文件缺少表格结构信息，无法计算。")

    # 目标 Sheet：查询点名优先；未点名时仅当"单文件多 Sheet"才澄清。
    sheet_name = _pick_sheet_name(normalized, sheets)
    if sheet_name is None:
        per_file: dict[str, set[str]] = {}
        for sheet in sheets:
            per_file.setdefault(sheet["source_path"], set()).add(sheet["sheet"])
        multi_sheet_files = {
            path for path, names in per_file.items() if len(names) > 1
        }
        if multi_sheet_files:
            names = "、".join(
                sorted(per_file[path] for path in sorted(multi_sheet_files))
            )
            return _clarify(
                f"检测到文件内多个 Sheet（{names}），请指定要对哪个 Sheet 计算。"
            )
        # 跨文件同名 Sheet：取第一个名字即可，各文件行会带自己的 sheet。
        sheet_name = next(
            (sheet["sheet"] for sheet in sheets if sheet.get("sheet")),
            None,
        )

    # 字段白名单：取目标 Sheet 的列。
    whitelist = _columns_for_sheet(sheets, sheet_name)

    operator = _parse_operator(normalized)

    # 目标列：查询点名匹配；聚合类未点名时用唯一数值列。
    column = _match_column(normalized, whitelist)
    if column_override and column_override in whitelist:
        column = column_override
    if operator in ("sum", "avg", "max", "min") and not column:
        numeric = _numeric_columns(sheets, sheet_name, whitelist)
        if len(numeric) == 1:
            column = numeric[0]
        elif not numeric:
            return _clarify(
                "没有找到数值列，无法求和/平均/最大/最小。可用列：" + _join_cols(whitelist)
            )
        else:
            return _clarify(
                "有多个数值列，请指定对哪一列计算。可用列：" + _join_cols(numeric),
                columns=numeric,
            )
    if operator in ("filter", "count") and not column and operator == "filter":
        return _clarify(
            "筛选条件不明确，请说明按哪一列、什么条件筛选。可用列：" + _join_cols(whitelist),
            columns=whitelist,
        )

    comparison, threshold = _parse_condition(normalized)
    if operator == "filter" and not comparison:
        return _clarify(
            "筛选需要明确的比较条件（如：超过 100）。可用列：" + _join_cols(whitelist),
            columns=whitelist,
        )

    # 分组/argmax：group 列 = 白名单里出现在查询中且 ≠ 目标列的列。
    group = _parse_group(normalized, whitelist, column)

    cross_file = bool(
        len(target_paths) > 1 or any(marker in normalized for marker in CROSS_FILE_MARKERS)
    )

    return ComputationSpec(
        operator=operator,
        column=column,
        comparison=comparison,
        threshold=threshold,
        group_by=group[0] if group else None,
        aggregate=group[1] if group else None,
        sheet=sheet_name,
        columns=tuple(whitelist),
        source_paths=tuple(target_paths),
        cross_file=cross_file,
    )


def resolve_rows(
    chunks: list[dict[str, Any]],
    nodes: dict[str, Any],
    spec: ComputationSpec,
) -> list[dict[str, Any]]:
    """从 row_group 节点重建完整行（规避 split_text 硬切），带 filename/sheet/row_number。"""
    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, int]] = set()
    for chunk in chunks:
        if chunk.get("node_type") != "row_group":
            continue
        source_path = str(chunk.get("source_path") or chunk.get("filename") or "")
        if source_path not in set(spec.source_paths):
            continue
        node = nodes.get(str(chunk.get("node_id") or ""))
        if node is None:
            continue
        grid_lines = str(node.content or "").split("\n")
        if not grid_lines or not grid_lines[0].strip():
            continue
        header = [col.strip() for col in grid_lines[0].split("\t")]
        row_numbers = list(
            node.metadata.get("row_numbers")
            or (chunk.get("metadata") or {}).get("row_numbers")
            or []
        )
        for index, line in enumerate(grid_lines[1:]):
            if not line.strip():
                continue
            cells_raw = line.split("\t")
            cells = {
                header[i]: cells_raw[i].strip()
                for i in range(min(len(header), len(cells_raw)))
            }
            row_number = row_numbers[index] if index < len(row_numbers) else None
            if row_number is None:
                continue
            key = (source_path, row_number)
            if key in seen:
                continue
            seen.add(key)
            rows.append(
                {
                    "row_number": row_number,
                    "cells": cells,
                    "filename": str(chunk.get("filename") or ""),
                    "sheet": str(chunk.get("page_or_sheet") or spec.sheet or ""),
                    "source_path": source_path,
                }
            )
    rows.sort(key=lambda item: (item["source_path"], item["row_number"]))
    if spec.cross_file:
        rows.sort(key=lambda item: (item["filename"], item["row_number"]))
    return rows


def compute_rows(
    rows: list[dict[str, Any]],
    spec: ComputationSpec,
) -> tuple[str, list[dict[str, Any]]]:
    if not rows:
        return "该表格没有可用数据。", []
    column = spec.column
    filtered = _apply_condition(rows, spec)
    if spec.operator == "filter":
        return _format_filter_result(filtered, spec)
    if spec.operator == "count":
        return f"共 {len(filtered)} 行", filtered
    # sum/avg/max/min / argmax
    if spec.group_by and spec.aggregate:
        return _compute_argmax(filtered, spec)
    values = [_numeric(value) for row in filtered for value in [_cell(row, column)]]
    values = [value for value in values if value is not None]
    if not values:
        return f"列“{column}”没有数值，无法计算。", []
    if spec.operator == "sum":
        total = sum(values)
        return f"{_label(spec)}合计 = {_fmt(total)}", filtered
    if spec.operator == "avg":
        avg = sum(values) / len(values)
        return f"{_label(spec)}平均值 = {_fmt(avg)}", filtered
    if spec.operator == "max":
        return f"{_label(spec)}最大值 = {_fmt(max(values))}", filtered
    if spec.operator == "min":
        return f"{_label(spec)}最小值 = {_fmt(min(values))}", filtered
    return "无法识别计算类型。", []


# ---------- 内部工具 ----------

def _apply_condition(rows: list[dict[str, Any]], spec: ComputationSpec) -> list[dict[str, Any]]:
    if not spec.comparison or spec.threshold is None:
        return rows
    column = spec.column
    result = []
    for row in rows:
        value = _numeric(_cell(row, column))
        if value is None:
            continue
        ok = {
            "gt": value > spec.threshold,
            "ge": value >= spec.threshold,
            "lt": value < spec.threshold,
            "le": value <= spec.threshold,
        }.get(spec.comparison, True)
        if ok:
            result.append(row)
    return result


def _compute_argmax(rows: list[dict[str, Any]], spec: ComputationSpec) -> tuple[str, list[dict]]:
    groups: dict[str, list[float]] = {}
    group_meta: dict[str, dict] = {}
    for row in rows:
        key = str(_cell(row, spec.group_by) or "?")
        value = _numeric(_cell(row, spec.column))
        if value is None:
            continue
        groups.setdefault(key, []).append(value)
        group_meta.setdefault(key, row)
    if not groups:
        return f"无法按“{spec.group_by}”分组计算。", []
    pick = max if spec.aggregate == "max" else min
    chosen_key = pick(groups, key=lambda k: pick(groups[k]))
    chosen_value = pick(groups[chosen_key])
    return (
        f"{spec.group_by}中“{spec.column}”{spec.aggregate}的是 {chosen_key}（{_fmt(chosen_value)}）",
        [group_meta[chosen_key]],
    )


def _format_filter_result(rows: list[dict[str, Any]], spec: ComputationSpec) -> tuple[str, list[dict]]:
    if not rows:
        threshold = _fmt(spec.threshold) if spec.threshold is not None else ""
        return f"没有满足条件（{spec.column} {spec.comparison} {threshold}）的行。", []
    parts = []
    for row in rows:
        name = _cell(row, "部门") or _cell(row, spec.group_by) or f"第{row['row_number']}行"
        parts.append(f"{name}（{_cell(row, spec.column)}）")
    return "满足条件：" + "、".join(parts), rows


def _label(spec: ComputationSpec) -> str:
    base = spec.sheet or "表格"
    column = spec.column or ""
    return f"{base}中“{column}”" if column else base


def _cell(row: dict[str, Any], column: Optional[str]) -> str:
    if not column:
        return ""
    cells = row.get("cells") or {}
    return str(cells.get(column) or "").strip()


def _numeric(text: str) -> Optional[float]:
    cleaned = str(text or "").strip().replace(",", "").replace("％", "%")
    if cleaned.endswith("%"):
        try:
            return float(cleaned[:-1]) / 100.0
        except ValueError:
            return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def _fmt(value: float) -> str:
    if abs(value - round(value)) < 1e-9:
        return str(int(round(value)))
    return f"{value:.4f}".rstrip("0").rstrip(".")


def _parse_operator(query: str) -> str:
    if any(m in query for m in SUM_MARKERS):
        return "sum"
    if any(m in query for m in AVG_MARKERS):
        return "avg"
    if any(m in query for m in MAX_MARKERS):
        return "max"
    if any(m in query for m in MIN_MARKERS):
        return "min"
    if any(m in query for m in COUNT_MARKERS):
        return "count"
    if any(m in query for m in FILTER_MARKERS) and re.search(r"\d", query):
        return "filter"
    return "count"


def _parse_group(
    query: str,
    whitelist: Iterable[str],
    target_column: Optional[str],
) -> tuple[Optional[str], Optional[str]]:
    if not any(marker in query for marker in ("最高", "最大", "最多", "最低", "最小", "最少")):
        return None, None
    aggregate = (
        "max"
        if any(marker in query for marker in ("最高", "最大", "最多"))
        else "min"
    )
    # 分组列 = 白名单中出现在查询里、且不是目标列的最长列名。
    for column in sorted(whitelist, key=len, reverse=True):
        if column and column != target_column and column in query:
            return column, aggregate
    return None, aggregate


def _parse_condition(query: str) -> tuple[Optional[str], Optional[float]]:
    match = _CONDITION_RE.search(query)
    if not match:
        return None, None
    word = match.group(1)
    comparison = _COMPARISON.get(word)
    if comparison is None:
        comparison = {"<=": "le", ">=": "ge", ">": "gt", "<": "lt"}.get(word)
    try:
        threshold = float(match.group(2).replace(",", "").replace("%", ""))
    except ValueError:
        return None, None
    return comparison, threshold


def _match_column(query: str, whitelist: Iterable[str]) -> Optional[str]:
    compact_query = re.sub(r"[\s._\-\\/：:（）()\[\]【】]+", "", query.casefold())
    best = None
    for column in whitelist:
        if not column:
            continue
        if column in query or re.sub(r"[\s.]+", "", column.casefold()) in compact_query:
            if best is None or len(column) > len(best):
                best = column
    return best


def _target_source_paths(
    query: str,
    chunks: list[dict[str, Any]],
    source_paths: tuple[str, ...],
) -> list[str]:
    all_tabular = sorted(
        {
            str(chunk.get("source_path") or chunk.get("filename") or "")
            for chunk in chunks
            if _suffix(chunk) in _TABULAR_SUFFIXES
        }
    )
    if source_paths:
        selected = [path for path in source_paths if path in all_tabular]
        return selected or all_tabular
    if any(marker in query for marker in CROSS_FILE_MARKERS):
        return all_tabular
    return all_tabular


def _sheet_summaries(
    chunks: list[dict[str, Any]],
    source_paths: list[str],
) -> list[dict[str, Any]]:
    result = []
    for chunk in chunks:
        # 只认 sheet_summary；workbook_summary 不含单表列结构，sheet 为空。
        if chunk.get("node_type") != "sheet_summary":
            continue
        source_path = str(chunk.get("source_path") or chunk.get("filename") or "")
        if source_path not in source_paths:
            continue
        metadata = chunk.get("metadata") or {}
        columns = list(metadata.get("columns") or [])
        sheet = str(chunk.get("page_or_sheet") or metadata.get("sheet") or "")
        if not sheet:
            sheet = str(chunk.get("filename") or "")
        result.append(
            {
                "filename": str(chunk.get("filename") or ""),
                "source_path": source_path,
                "sheet": sheet,
                "columns": columns,
                "column_types": metadata.get("column_types") or {},
            }
        )
    return result


def _pick_sheet_name(query: str, sheets: list[dict[str, Any]]) -> Optional[str]:
    if not sheets:
        return None
    candidates = {sheet["sheet"] for sheet in sheets if sheet.get("sheet")}
    for name in candidates:
        if name and name in query:
            return name
    return None


def _columns_for_sheet(sheets: list[dict[str, Any]], sheet_name: Optional[str]) -> list[str]:
    for sheet in sheets:
        if sheet.get("sheet") == sheet_name:
            return list(sheet["columns"])
    # 退化为第一个 sheet 的列
    return list(sheets[0]["columns"]) if sheets else []


def _numeric_columns(
    sheets: list[dict[str, Any]],
    sheet_name: Optional[str],
    whitelist: list[str],
) -> list[str]:
    for sheet in sheets:
        if sheet.get("sheet") != sheet_name:
            continue
        column_types = sheet.get("column_types") or {}
        return [
            column
            for column in whitelist
            if str(column_types.get(column) or "").lower() in ("int", "float", "numeric", "number")
        ]
    return []


def _suffix(chunk: dict[str, Any]) -> str:
    value = str(chunk.get("filename") or chunk.get("source_path") or "")
    return "." + value.rsplit(".", 1)[-1].lower() if "." in value else ""


def _join_cols(columns: Iterable[str]) -> str:
    return "、".join(str(c) for c in columns)


def replace(spec: ComputationSpec, **kwargs: Any) -> ComputationSpec:
    return ComputationSpec(
        **{**spec.__dict__, **kwargs}
    )


def _clarify(
    message: str,
    columns: Iterable[str] = (),
) -> ComputationSpec:
    return ComputationSpec(clarify=message, columns=tuple(columns))
