from __future__ import annotations

from typing import Any


def render_markdown_table(rows: list[list[str]]) -> str:
    """把二维行渲染成 Markdown 表格，用于表格展示文本的延迟生成。"""
    if not rows:
        return ""
    column_count = max((len(row) for row in rows), default=0)
    normalized = [
        [str(cell) if cell is not None else "" for cell in row]
        + [""] * (column_count - len(row))
        for row in rows
    ]
    rendered: list[str] = []
    for index, row in enumerate(normalized):
        cells = " | ".join(_escape_markdown_cell(cell) for cell in row)
        rendered.append(f"| {cells} |")
        if index == 0:
            rendered.append("| " + " | ".join(["---"] * column_count) + " |")
    return "\n".join(rendered)


def _escape_markdown_cell(value: Any) -> str:
    return (
        str(value if value is not None else "")
        .replace("|", "\\|")
        .replace("\n", " ")
        .replace("\r", " ")
    )


def render_table_display(record: dict) -> str | None:
    """从保存的表格结构（metadata.cells）延迟生成展示文本。

    索引时只保存结构，展示文本按需生成，避免为每个表格节点提前渲染。
    """
    metadata = record.get("metadata") or {}
    cells = metadata.get("cells")
    if not isinstance(cells, list) or not cells:
        return None
    rows = []
    for row in cells:
        if not isinstance(row, (list, tuple)):
            continue
        rows.append([str(cell) if cell is not None else "" for cell in row])
    if not rows:
        return None
    return render_markdown_table(rows)
