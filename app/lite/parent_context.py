"""P1-1 Parent-Child 自适应检索：查询时把命中的小块扩展到父节点内容。

设计目标：小块用于检索（保持低成本 BM25），命中后把父章节/父表格内容
一起送入生成阶段，让答案有完整上下文。本模块只负责“查父节点内容”，
不改变检索排序；检索、路由、生成的分工保持不变。

实现要点：
- 分片感知：只读取命中文档对应分片的 nodes.jsonl / parents.jsonl，
  绝不 O(全部节点)。
- 懒加载：每个 document_id 的分片只读一次并缓存。
- 表格父节点用 effective_display_content（渲染后的 Markdown 表格），
  文本父节点用 content。
- 带单父字符上限与累计上限，避免膨胀生成上下文。
- 优雅降级：缺 parent_id / 缺文档 / 缺父记录 / 内容为空时不附加，
  不抛异常。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

from app.documents.node import DocumentNode
from app.lite.indexer import (
    NODES_FILE,
    PARENTS_FILE,
    _iter_jsonl_records,
    _resolve_shard_path,
)


class ParentContextResolver:
    """把检索命中的 sources 就地富化出父节点内容。"""

    def __init__(
        self,
        index_dir: str | Path,
        *,
        max_parent_chars: int = 8000,
        max_total_chars: int = 40000,
    ) -> None:
        self._index_path = Path(index_dir).expanduser().resolve()
        self._max_parent_chars = int(max_parent_chars)
        self._max_total_chars = int(max_total_chars)
        self._shard_map: dict[str, Optional[str]] | None = None
        self._nodes_cache: dict[str, dict[str, DocumentNode]] = {}
        self._parents_cache: dict[str, dict[str, dict[str, Any]]] = {}

    def resolve(
        self,
        sources: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """就地附加 parent_* 字段并返回 sources。

        每个 source 附加：parent_content、parent_display_content（不同时）、
        parent_node_id。child 的 content / chunk_index 保持不变，
        因此引用回溯与抽取式回答不受影响。
        """
        if not sources:
            return sources
        shard_map = self._document_shard_map()
        total = 0
        seen_parents: set[tuple[str, str]] = set()
        for source in sources:
            parent = self._parent_for(source, shard_map)
            if parent is None:
                continue
            text = parent[0]
            display = parent[1]
            if not text.strip():
                continue
            if _same_trimmed(text, str(source.get("content") or "")):
                continue
            document_id = str(source.get("document_id") or "")
            parent_id = str(source.get("parent_id") or "")
            parent_key = (document_id, parent_id)
            # 同一父只附加一次：多个命中 chunk 同属一个父时不重复带入整段父内容。
            if parent_key in seen_parents:
                continue
            seen_parents.add(parent_key)
            text = _truncate(text, self._max_parent_chars)
            if total + len(text) > self._max_total_chars:
                continue
            total += len(text)
            source["parent_content"] = text
            if display and display.strip() and display != text:
                source["parent_display_content"] = display
            source["parent_node_id"] = str(
                source.get("parent_id") or source.get("node_id") or ""
            )
        return sources

    # ---------- 内部 ----------

    def _document_shard_map(self) -> dict[str, Optional[str]]:
        if self._shard_map is not None:
            return self._shard_map
        result: dict[str, Optional[str]] = {}
        manifest_path = self._index_path / "manifest.json"
        if manifest_path.exists():
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                manifest = {}
            for document in manifest.get("documents") or []:
                document_id = str(document.get("document_id") or "")
                if document_id:
                    raw = document.get("shard_path")
                    result[document_id] = str(raw) if raw else None
        self._shard_map = result
        return result

    def _shard_base(
        self,
        document_id: str,
        shard_map: dict[str, Optional[str]],
    ) -> Optional[Path]:
        if document_id not in shard_map:
            return None
        relative = shard_map[document_id]
        if not relative:
            # 旧单体布局：nodes/parents/chunks 在索引顶层。
            return self._index_path
        return _resolve_shard_path(self._index_path, relative)

    def _nodes_for(
        self,
        document_id: str,
        base: Path,
    ) -> dict[str, DocumentNode]:
        cached = self._nodes_cache.get(document_id)
        if cached is not None:
            return cached
        nodes: dict[str, DocumentNode] = {}
        for record in _iter_jsonl_records(base / NODES_FILE, "DocumentNode"):
            try:
                node = DocumentNode.from_record(record)
            except (KeyError, TypeError, ValueError):
                continue
            nodes[node.node_id] = node
        self._nodes_cache[document_id] = nodes
        return nodes

    def _parents_for(
        self,
        document_id: str,
        base: Path,
    ) -> dict[str, dict[str, Any]]:
        cached = self._parents_cache.get(document_id)
        if cached is not None:
            return cached
        parents: dict[str, dict[str, Any]] = {}
        for record in _iter_jsonl_records(base / PARENTS_FILE, "Parent"):
            parents[str(record.get("parent_id") or "")] = dict(record)
        self._parents_cache[document_id] = parents
        return parents

    def _parent_for(
        self,
        source: dict[str, Any],
        shard_map: dict[str, Optional[str]],
    ) -> Optional[tuple[str, Optional[str]]]:
        document_id = str(source.get("document_id") or "")
        parent_id = str(source.get("parent_id") or "")
        if not document_id or not parent_id:
            return None
        base = self._shard_base(document_id, shard_map)
        if base is None:
            return None
        parent = self._parents_for(document_id, base).get(parent_id)
        if not parent:
            return None
        content_node_id = str(parent.get("content_node_id") or "")
        node = self._nodes_for(document_id, base).get(content_node_id)
        if node is None:
            return None
        return (node.effective_display_content, node.display_content)


def _same_trimmed(left: str, right: str) -> bool:
    return bool(left.strip()) and left.strip() == right.strip()


def _truncate(text: str, limit: int) -> str:
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    return text[:limit]
