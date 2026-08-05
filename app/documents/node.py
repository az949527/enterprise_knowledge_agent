from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import hashlib
import json
from pathlib import PurePosixPath
from typing import Any, Mapping, Optional, Sequence, Union


DOCUMENT_NODE_SCHEMA_VERSION = 1
PageOrSheet = Optional[Union[int, str]]


class NodeType(str, Enum):
    TEXT = "text"
    TABLE = "table"
    FIGURE = "figure"
    WORKBOOK_SUMMARY = "workbook_summary"
    SHEET_SUMMARY = "sheet_summary"
    ROW_GROUP = "row_group"


@dataclass(frozen=True, slots=True)
class BoundingBox:
    x0: float
    y0: float
    x1: float
    y1: float

    def __post_init__(self) -> None:
        if self.x1 < self.x0 or self.y1 < self.y0:
            raise ValueError("BoundingBox end coordinates must not precede start coordinates.")

    def to_list(self) -> list[float]:
        return [self.x0, self.y0, self.x1, self.y1]

    @classmethod
    def from_value(
        cls,
        value: Optional[
            Union["BoundingBox", Sequence[float], Mapping[str, float]]
        ],
    ) -> Optional["BoundingBox"]:
        if value is None or isinstance(value, cls):
            return value
        if isinstance(value, Mapping):
            return cls(
                float(value["x0"]),
                float(value["y0"]),
                float(value["x1"]),
                float(value["y1"]),
            )
        if len(value) != 4:
            raise ValueError("bbox must contain exactly four coordinates.")
        return cls(*(float(item) for item in value))


@dataclass(slots=True)
class DocumentNode:
    document_id: str
    content: str
    parser_version: str
    node_type: NodeType = NodeType.TEXT
    node_id: str = ""
    content_hash: str = ""
    page_or_sheet: PageOrSheet = None
    section_path: tuple[str, ...] = ()
    sequence: int = 0
    bbox: Optional[BoundingBox] = None
    row_start: Optional[int] = None
    row_end: Optional[int] = None
    column_start: Optional[int] = None
    column_end: Optional[int] = None
    parent_id: Optional[str] = None
    display_content: Optional[str] = None
    source_anchor: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    schema_version: int = DOCUMENT_NODE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        self.document_id = str(self.document_id or "").strip()
        self.content = str(self.content or "")
        self.parser_version = str(self.parser_version or "").strip()
        self.node_type = NodeType(self.node_type)
        self.section_path = tuple(str(item) for item in self.section_path)
        self.bbox = BoundingBox.from_value(self.bbox)
        self.source_anchor = dict(self.source_anchor or {})
        self.metadata = dict(self.metadata or {})
        if not self.document_id:
            raise ValueError("document_id is required.")
        if not self.parser_version:
            raise ValueError("parser_version is required.")
        if self.sequence < 0:
            raise ValueError("sequence must be non-negative.")
        _validate_range("row", self.row_start, self.row_end)
        _validate_range("column", self.column_start, self.column_end)
        if self.display_content == self.content:
            self.display_content = None
        calculated_hash = content_sha256(self.content)
        if self.content_hash and self.content_hash != calculated_hash:
            raise ValueError("content_hash does not match content.")
        self.content_hash = calculated_hash
        if not self.node_id:
            self.node_id = self._build_node_id()

    @property
    def search_content(self) -> str:
        return self.content

    @property
    def effective_display_content(self) -> str:
        return self.display_content if self.display_content is not None else self.content

    def to_record(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "node_id": self.node_id,
            "document_id": self.document_id,
            "content_hash": self.content_hash,
            "parser_version": self.parser_version,
            "node_type": self.node_type.value,
            "page_or_sheet": self.page_or_sheet,
            "section_path": list(self.section_path),
            "sequence": self.sequence,
            "bbox": self.bbox.to_list() if self.bbox else None,
            "row_start": self.row_start,
            "row_end": self.row_end,
            "column_start": self.column_start,
            "column_end": self.column_end,
            "parent_id": self.parent_id,
            "content": self.content,
            "display_content": self.display_content,
            "source_anchor": dict(self.source_anchor),
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_record(cls, record: Mapping[str, Any]) -> "DocumentNode":
        return cls(
            schema_version=int(
                record.get("schema_version") or DOCUMENT_NODE_SCHEMA_VERSION
            ),
            node_id=str(record.get("node_id") or ""),
            document_id=str(record.get("document_id") or ""),
            content_hash=str(record.get("content_hash") or ""),
            parser_version=str(record.get("parser_version") or ""),
            node_type=NodeType(record.get("node_type") or NodeType.TEXT.value),
            page_or_sheet=record.get("page_or_sheet"),
            section_path=tuple(record.get("section_path") or ()),
            sequence=int(record.get("sequence") or 0),
            bbox=BoundingBox.from_value(record.get("bbox")),
            row_start=_optional_int(record.get("row_start")),
            row_end=_optional_int(record.get("row_end")),
            column_start=_optional_int(record.get("column_start")),
            column_end=_optional_int(record.get("column_end")),
            parent_id=_optional_str(record.get("parent_id")),
            content=str(record.get("content") or ""),
            display_content=_optional_str(record.get("display_content")),
            source_anchor=dict(record.get("source_anchor") or {}),
            metadata=dict(record.get("metadata") or {}),
        )

    def _build_node_id(self) -> str:
        identity = {
            "document_id": self.document_id,
            "parser_version": self.parser_version,
            "node_type": self.node_type.value,
            "sequence": self.sequence,
            "page_or_sheet": self.page_or_sheet,
            "section_path": self.section_path,
            "source_anchor": self.source_anchor,
            "content_hash": self.content_hash,
        }
        payload = json.dumps(
            identity,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        return "node_" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


def content_sha256(content: str) -> str:
    return hashlib.sha256(str(content).encode("utf-8")).hexdigest()


def document_id_from_source(source_path: str) -> str:
    normalized = PurePosixPath(str(source_path).replace("\\", "/")).as_posix()
    digest = hashlib.sha256(normalized.casefold().encode("utf-8")).hexdigest()
    return "doc_" + digest[:24]


def _validate_range(
    label: str,
    start: Optional[int],
    end: Optional[int],
) -> None:
    if start is not None and start < 0:
        raise ValueError(f"{label}_start must be non-negative.")
    if end is not None and end < 0:
        raise ValueError(f"{label}_end must be non-negative.")
    if start is not None and end is not None and end < start:
        raise ValueError(f"{label}_end must not precede {label}_start.")


def _optional_int(value: Any) -> Optional[int]:
    return None if value is None else int(value)


def _optional_str(value: Any) -> Optional[str]:
    return None if value is None else str(value)
