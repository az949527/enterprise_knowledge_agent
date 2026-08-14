from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from datetime import datetime
from itertools import chain
from pathlib import Path, PurePosixPath
import shutil
from typing import Any, Callable, Iterable, Iterator, Mapping, Optional
from uuid import uuid4

from app.documents import (
    CSV_PARSER_VERSION,
    DOCUMENT_NODE_SCHEMA_VERSION,
    DOCUMENT_PARSER_INTERFACE_VERSION,
    DocumentNode,
    NodeType,
    PDF_PARSER_VERSION,
    SUPPORTED_DOCUMENT_EXTENSIONS,
    TEXT_PARSER_VERSION,
    XLSX_PARSER_VERSION,
    CsvEncodingError,
    ParserError,
    content_sha256,
    document_id_from_source,
    iter_document_nodes,
    iter_document_nodes_from_bytes,
)
from app.documents.table_display import render_table_display


SUPPORTED_EXTENSIONS = set(SUPPORTED_DOCUMENT_EXTENSIONS)
DEFAULT_INDEX_DIR = Path("data/lite_index")
LITE_PARSER_VERSION = TEXT_PARSER_VERSION
INDEX_FORMAT_VERSION = 3
NODES_FILE = "nodes.jsonl"
PARENTS_FILE = "parents.jsonl"
INDEX_MANIFEST_FILE = "manifest.json"
INDEX_TRANSACTION_FILE = ".index-transaction.json"
STORAGE_LAYOUT_FIELD = "storage_layout"
MONOLITHIC_STORAGE_LAYOUT = "monolithic_v1"
SHARDED_STORAGE_LAYOUT = "document_shards_v1"
SHARDS_DIR = "shards"
SHARD_PATH_FIELD = "shard_path"
SOURCE_SHA256_FIELD = "source_sha256"
SOURCE_SIZE_FIELD = "source_size"
SOURCE_MTIME_NS_FIELD = "source_mtime_ns"
STRUCTURAL_CHUNK_FIELDS = (
    "schema_version",
    "content_hash",
    "node_id",
    "document_id",
    "node_content_hash",
    "parser_version",
    "node_type",
    "page_or_sheet",
    "section_path",
    "sequence",
    "bbox",
    "row_start",
    "row_end",
    "column_start",
    "column_end",
    "parent_id",
    "source_anchor",
    "metadata",
    "display_content",
)


@dataclass
class LiteIndexStats:
    source_dir: str
    index_dir: str
    file_count: int
    chunk_count: int
    added_count: int = 0
    updated_count: int = 0
    removed_count: int = 0
    skipped_count: int = 0
    failed_count: int = 0
    documents: list[dict] = field(default_factory=list)
    skipped_files: list[str] = field(default_factory=list)
    failed_files: list[dict[str, str]] = field(default_factory=list)


class IndexFormatError(RuntimeError):
    pass


class IndexCancelledError(RuntimeError):
    pass


ProgressCallback = Callable[[dict[str, Any]], None]
CancelCheck = Callable[[], bool]


def chunk_structure(record: dict) -> dict:
    result = {
        key: record.get(key)
        for key in STRUCTURAL_CHUNK_FIELDS
        if key in record
    }
    # 表格展示文本延迟生成：索引只保存结构，查询命中时才渲染。
    if result.get("node_type") == "table" and result.get("display_content") is None:
        display = render_table_display(record)
        if display:
            result["display_content"] = display
    return result


def chunk_search_text(record: Mapping[str, Any]) -> str:
    stored = str(record.get("search_text") or "").strip()
    if stored:
        return stored
    return _search_text(
        content=str(record.get("content") or ""),
        filename=str(record.get("filename") or ""),
        source_path=str(record.get("source_path") or ""),
        node_type=str(record.get("node_type") or ""),
        page_or_sheet=record.get("page_or_sheet"),
        metadata=record.get("metadata"),
    )


def chunk_fingerprint_text(record: Mapping[str, Any]) -> str:
    return "\0".join(
        (
            str(record.get("content") or ""),
            str(record.get("filename") or ""),
            str(record.get("node_type") or ""),
            str(record.get("page_or_sheet") or ""),
        )
    )


def build_index(
    source_dir: str | Path,
    index_dir: str | Path = DEFAULT_INDEX_DIR,
    chunk_size: int = 900,
    chunk_overlap: int = 120,
) -> LiteIndexStats:
    source_path = Path(source_dir).expanduser().resolve()
    if not source_path.exists() or not source_path.is_dir():
        raise FileNotFoundError(f"Source directory does not exist: {source_path}")

    return sync_index_paths(
        list(iter_supported_files(source_path)),
        index_dir=index_dir,
        source_root=source_path,
        source_label=source_path.as_posix(),
        remove_missing=True,
        chunk_size=chunk_size,
        chunk_overlap=chunk_overlap,
    )


def sync_index_paths(
    paths: Iterable[str | Path],
    index_dir: str | Path = DEFAULT_INDEX_DIR,
    *,
    source_root: str | Path | None = None,
    source_label: str = "desktop_upload",
    remove_missing: bool = False,
    force_reparse: bool = False,
    csv_encodings: Mapping[str, str] | None = None,
    isolate_failures: bool = True,
    chunk_size: int = 900,
    chunk_overlap: int = 120,
    progress: ProgressCallback | None = None,
    should_cancel: CancelCheck | None = None,
) -> LiteIndexStats:
    output_dir = Path(index_dir).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    recover_index_transaction(output_dir)
    manifest_path = output_dir / INDEX_MANIFEST_FILE
    if manifest_path.exists():
        try:
            ensure_index_format(output_dir)
        except IndexFormatError:
            _archive_incompatible_index(output_dir)
            output_dir.mkdir(parents=True, exist_ok=True)

    resolved_root = (
        Path(source_root).expanduser().resolve()
        if source_root is not None
        else None
    )
    resolved_paths = [Path(path).expanduser().resolve() for path in paths]
    existing_documents = list_index_documents(output_dir)
    existing_by_source = {
        normalize_upload_name(
            str(document.get("source_path") or document.get("filename") or "")
        ).casefold(): document
        for document in existing_documents
    }
    encoding_overrides = {
        str(Path(path).expanduser().resolve()): encoding
        for path, encoding in dict(csv_encodings or {}).items()
    }

    staged_documents: list[dict[str, Any]] = []
    staged_paths: list[Path] = []
    failed_files: list[dict[str, str]] = []
    skipped_files: list[str] = []
    updated_source_keys: set[str] = set()
    current_source_keys: set[str] = set()
    added_count = 0
    updated_count = 0

    try:
        total = len(resolved_paths)
        for position, path in enumerate(resolved_paths, start=1):
            _raise_if_cancelled(should_cancel)
            source_path = _relative_source_path(path, resolved_root)
            source_key = source_path.casefold()
            current_source_keys.add(source_key)
            _emit_progress(
                progress,
                phase="fingerprint",
                current=position,
                total=total,
                filename=path.name,
                source_path=source_path,
            )
            try:
                if not path.is_file():
                    raise FileNotFoundError(f"Document file does not exist: {path}")
                if path.suffix.lower() not in SUPPORTED_EXTENSIONS:
                    raise ParserError(
                        f"Unsupported document extension: {path.suffix.lower()}"
                    )
                source_record = _source_record_for_path(
                    path,
                    source_path,
                    should_cancel=should_cancel,
                )
                existing = existing_by_source.get(source_key)
                if (
                    not force_reparse
                    and
                    existing
                    and existing.get(SOURCE_SHA256_FIELD)
                    == source_record[SOURCE_SHA256_FIELD]
                ):
                    skipped_files.append(path.name)
                    _emit_progress(
                        progress,
                        phase="skipped",
                        current=position,
                        total=total,
                        filename=path.name,
                        source_path=source_path,
                    )
                    continue

                stage_path = output_dir / f".{uuid4().hex}.nodes.jsonl.tmp"
                staged_paths.append(stage_path)
                node_count = 0
                document_id = ""
                _emit_progress(
                    progress,
                    phase="parsing",
                    current=position,
                    total=total,
                    filename=path.name,
                    source_path=source_path,
                )
                with stage_path.open("w", encoding="utf-8") as writer:
                    for node in iter_document_nodes(
                        path,
                        source_path=source_path,
                        csv_encoding=encoding_overrides.get(str(path)),
                    ):
                        _raise_if_cancelled(should_cancel)
                        document_id = node.document_id
                        writer.write(
                            json.dumps(node.to_record(), ensure_ascii=False)
                            + "\n"
                        )
                        node_count += 1
                if node_count == 0:
                    raise ParserError(f"No indexable content found in {path.name}.")
                staged_documents.append(
                    {
                        "document_id": document_id,
                        "source_path": source_path,
                        "source_key": source_key,
                        "stage_path": stage_path,
                        "source_record": source_record,
                    }
                )
                updated_source_keys.add(source_key)
                if existing:
                    updated_count += 1
                else:
                    added_count += 1
                _emit_progress(
                    progress,
                    phase="parsed",
                    current=position,
                    total=total,
                    filename=path.name,
                    source_path=source_path,
                )
            except CsvEncodingError:
                raise
            except IndexCancelledError:
                raise
            except Exception as exc:
                if not isolate_failures:
                    raise
                failed_files.append(
                    {
                        "filename": path.name,
                        "source_path": source_path,
                        "error_type": type(exc).__name__,
                        "error": str(exc),
                    }
                )
                _emit_progress(
                    progress,
                    phase="failed",
                    current=position,
                    total=total,
                    filename=path.name,
                    source_path=source_path,
                    error=str(exc),
                )

        removed_source_keys = (
            set(existing_by_source) - current_source_keys
            if remove_missing
            else set()
        )
        replaced_source_keys = updated_source_keys | removed_source_keys
        if not staged_documents and not removed_source_keys:
            return LiteIndexStats(
                source_dir=source_label,
                index_dir=output_dir.as_posix(),
                file_count=len(existing_documents),
                chunk_count=sum(
                    int(document.get("chunk_count") or 0)
                    for document in existing_documents
                ),
                skipped_count=len(skipped_files),
                failed_count=len(failed_files),
                documents=existing_documents,
                skipped_files=skipped_files,
                failed_files=failed_files,
            )

        _emit_progress(
            progress,
            phase="committing",
            current=len(resolved_paths),
            total=len(resolved_paths),
            filename="",
            source_path="",
        )
        stats = _commit_sharded_sync(
            existing_documents=existing_documents,
            staged_documents=staged_documents,
            removed_source_keys=removed_source_keys,
            source_label=source_label,
            output_dir=output_dir,
            chunk_size=chunk_size,
            chunk_overlap=chunk_overlap,
            should_cancel=should_cancel,
        )
        stats.added_count = added_count
        stats.updated_count = updated_count
        stats.removed_count = len(removed_source_keys)
        stats.skipped_count = len(skipped_files)
        stats.failed_count = len(failed_files)
        stats.skipped_files = skipped_files
        stats.failed_files = failed_files
        _emit_progress(
            progress,
            phase="completed",
            current=len(resolved_paths),
            total=len(resolved_paths),
            filename="",
            source_path="",
        )
        return stats
    finally:
        _remove_files(staged_paths)


def rebuild_index(
    source_dir: str | Path,
    index_dir: str | Path = DEFAULT_INDEX_DIR,
    *,
    csv_encodings: Mapping[str, str] | None = None,
    chunk_size: int = 900,
    chunk_overlap: int = 120,
    progress: ProgressCallback | None = None,
    should_cancel: CancelCheck | None = None,
) -> LiteIndexStats:
    source_path = Path(source_dir).expanduser().resolve()
    if not source_path.exists() or not source_path.is_dir():
        raise FileNotFoundError(f"Source directory does not exist: {source_path}")
    return sync_index_paths(
        list(iter_supported_files(source_path)),
        index_dir=index_dir,
        source_root=source_path,
        source_label=source_path.as_posix(),
        remove_missing=True,
        force_reparse=True,
        csv_encodings=csv_encodings,
        isolate_failures=False,
        chunk_size=chunk_size,
        chunk_overlap=chunk_overlap,
        progress=progress,
        should_cancel=should_cancel,
    )


def _commit_sharded_sync(
    *,
    existing_documents: list[dict],
    staged_documents: list[dict[str, Any]],
    removed_source_keys: set[str],
    source_label: str,
    output_dir: Path,
    chunk_size: int,
    chunk_overlap: int,
    should_cancel: CancelCheck | None,
) -> LiteIndexStats:
    token = uuid4().hex
    stage_root = output_dir / f".shard-stage-{token}"
    stage_root.mkdir(parents=True, exist_ok=True)
    shards_root = output_dir / SHARDS_DIR
    shards_root.mkdir(parents=True, exist_ok=True)
    staged_source_keys = {
        str(staged["source_key"]) for staged in staged_documents
    }
    documents = [
        dict(document)
        for document in existing_documents
        if normalize_upload_name(
            str(document.get("source_path") or document.get("filename") or "")
        ).casefold()
        not in removed_source_keys | staged_source_keys
    ]
    staged_shards: dict[str, Path] = {}
    commit_pairs: list[tuple[Path, Path]] = []
    try:
        for staged in staged_documents:
            _raise_if_cancelled(should_cancel)
            document_id = str(staged["document_id"])
            shard_stage = stage_root / document_id

            def staged_nodes(
                stage_path: Path = staged["stage_path"],
            ) -> Iterator[DocumentNode]:
                for record in _iter_jsonl_records(
                    stage_path,
                    "Staged DocumentNode",
                ):
                    _raise_if_cancelled(should_cancel)
                    yield DocumentNode.from_record(record)

            shard_stats = write_node_index(
                staged_nodes(),
                source_label=source_label,
                index_dir=shard_stage,
                chunk_size=chunk_size,
                chunk_overlap=chunk_overlap,
                document_sources={
                    str(staged["source_key"]): staged["source_record"]
                },
                should_cancel=should_cancel,
            )
            if len(shard_stats.documents) != 1:
                raise IndexFormatError(
                    f"Document shard build produced "
                    f"{len(shard_stats.documents)} documents for "
                    f"{staged['source_path']}."
                )
            shard_relative = (
                PurePosixPath(SHARDS_DIR) / document_id
            ).as_posix()
            document_record = dict(shard_stats.documents[0])
            document_record[SHARD_PATH_FIELD] = shard_relative
            documents.append(document_record)
            staged_shards[document_id] = shard_stage
            commit_pairs.append(
                (shard_stage, output_dir / shard_relative)
            )

        documents.sort(
            key=lambda item: str(
                item.get("source_path") or item.get("filename") or ""
            ).casefold()
        )
        fingerprint = hashlib.sha256()
        for chunk in _iter_records_for_documents(
            output_dir,
            documents,
            "chunks.jsonl",
            "Chunk",
            staged_shards=staged_shards,
        ):
            fingerprint.update(
                str(chunk.get("id") or "").encode("utf-8")
            )
            fingerprint.update(b"\0")
            fingerprint.update(chunk_fingerprint_text(chunk).encode("utf-8"))
            fingerprint.update(b"\0")

        parser_versions = sorted(
            {
                str(version)
                for document in documents
                for version in (document.get("parser_versions") or [])
            }
        )
        node_types = sorted(
            {
                str(node_type)
                for document in documents
                for node_type in (document.get("node_types") or [])
            }
        )
        manifest = {
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "index_format_version": INDEX_FORMAT_VERSION,
            "document_parser_interface_version": DOCUMENT_PARSER_INTERFACE_VERSION,
            "source_dir": source_label,
            "index_dir": output_dir.as_posix(),
            "file_count": len(documents),
            "chunk_count": sum(
                int(document.get("chunk_count") or 0)
                for document in documents
            ),
            "extensions": sorted(SUPPORTED_EXTENSIONS),
            "chunk_size": chunk_size,
            "chunk_overlap": chunk_overlap,
            "index_fingerprint": fingerprint.hexdigest(),
            "document_node_schema_version": DOCUMENT_NODE_SCHEMA_VERSION,
            "parser_versions": parser_versions,
            "node_types": node_types,
            "nodes_file": NODES_FILE,
            "parents_file": PARENTS_FILE,
            "chunks_file": "chunks.jsonl",
            STORAGE_LAYOUT_FIELD: SHARDED_STORAGE_LAYOUT,
            "shards_dir": SHARDS_DIR,
            "documents": documents,
        }
        manifest_temp = output_dir / f"{INDEX_MANIFEST_FILE}.tmp"
        manifest_temp.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        commit_pairs.append(
            (manifest_temp, output_dir / INDEX_MANIFEST_FILE)
        )
        _raise_if_cancelled(should_cancel)
        _commit_index_files(commit_pairs)
        _cleanup_unreferenced_shards(output_dir, documents)
        if not any(
            not document.get(SHARD_PATH_FIELD)
            for document in documents
        ):
            _write_empty_compatibility_files(output_dir)
        return LiteIndexStats(
            source_dir=source_label,
            index_dir=output_dir.as_posix(),
            file_count=len(documents),
            chunk_count=int(manifest["chunk_count"]),
            added_count=len(staged_documents),
            documents=documents,
        )
    finally:
        _remove_path(stage_root)


def _iter_records_for_documents(
    index_path: Path,
    documents: list[dict],
    filename: str,
    label: str,
    *,
    staged_shards: Mapping[str, Path] | None = None,
) -> Iterator[dict]:
    stage_map = dict(staged_shards or {})
    legacy_document_ids = {
        str(document.get("document_id") or "")
        for document in documents
        if not document.get(SHARD_PATH_FIELD)
    }
    if legacy_document_ids:
        for record in _iter_jsonl_records(index_path / filename, label):
            if str(record.get("document_id") or "") in legacy_document_ids:
                yield record
    for document in documents:
        shard_relative = document.get(SHARD_PATH_FIELD)
        if not shard_relative:
            continue
        document_id = str(document.get("document_id") or "")
        shard_path = stage_map.get(document_id)
        if shard_path is None:
            shard_path = _resolve_shard_path(
                index_path,
                str(shard_relative),
            )
        yield from _iter_jsonl_records(shard_path / filename, label)


def _cleanup_unreferenced_shards(
    index_path: Path,
    documents: list[dict],
) -> None:
    shards_root = index_path / SHARDS_DIR
    if not shards_root.exists():
        return
    referenced = {
        _resolve_shard_path(index_path, str(document[SHARD_PATH_FIELD]))
        for document in documents
        if document.get(SHARD_PATH_FIELD)
    }
    for path in shards_root.iterdir():
        if path.resolve() not in referenced:
            _remove_path(path)


def _write_empty_compatibility_files(index_path: Path) -> None:
    for name in (NODES_FILE, PARENTS_FILE, "chunks.jsonl"):
        path = index_path / name
        path.write_text("", encoding="utf-8")


def build_index_from_uploads(
    documents: Iterable[tuple[str, str]],
    index_dir: str | Path = DEFAULT_INDEX_DIR,
    chunk_size: int = 900,
    chunk_overlap: int = 120,
) -> LiteIndexStats:
    return append_node_index(
        _legacy_documents_to_nodes(documents),
        source_label="browser_upload",
        index_dir=index_dir,
        chunk_size=chunk_size,
        chunk_overlap=chunk_overlap,
    )


def build_index_from_nodes(
    nodes: Iterable[DocumentNode],
    index_dir: str | Path = DEFAULT_INDEX_DIR,
    chunk_size: int = 900,
    chunk_overlap: int = 120,
    *,
    source_label: str = "desktop_upload",
) -> LiteIndexStats:
    return append_node_index(
        nodes,
        source_label=source_label,
        index_dir=index_dir,
        chunk_size=chunk_size,
        chunk_overlap=chunk_overlap,
    )


def write_index(
    documents: Iterable[tuple[str, str]],
    *,
    source_label: str,
    index_dir: str | Path,
    chunk_size: int,
    chunk_overlap: int,
) -> LiteIndexStats:
    return write_node_index(
        _legacy_documents_to_nodes(documents),
        source_label=source_label,
        index_dir=index_dir,
        chunk_size=chunk_size,
        chunk_overlap=chunk_overlap,
    )


def write_node_index(
    nodes: Iterable[DocumentNode],
    *,
    source_label: str,
    index_dir: str | Path,
    chunk_size: int,
    chunk_overlap: int,
    document_sources: Mapping[str, Mapping[str, Any]] | None = None,
    should_cancel: CancelCheck | None = None,
) -> LiteIndexStats:
    output_dir = Path(index_dir).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    recover_index_transaction(output_dir)
    chunks_temp = output_dir / "chunks.jsonl.tmp"
    nodes_temp = output_dir / f"{NODES_FILE}.tmp"
    parents_temp = output_dir / f"{PARENTS_FILE}.tmp"
    manifest_temp = output_dir / f"{INDEX_MANIFEST_FILE}.tmp"
    temporary_paths = (
        nodes_temp,
        parents_temp,
        chunks_temp,
        manifest_temp,
    )

    chunk_count = 0
    chunk_indices: dict[str, int] = {}
    document_stats: dict[str, dict] = {}
    parser_versions: set[str] = set()
    node_types: set[str] = set()
    index_fingerprint = hashlib.sha256()
    written_parents: set[str] = set()
    active_document_id: Optional[str] = None
    ordered_nodes = _ordered_nodes(nodes)
    source_records = {
        str(key).casefold(): dict(value)
        for key, value in dict(document_sources or {}).items()
    }
    try:
        with (
            chunks_temp.open("w", encoding="utf-8") as chunk_writer,
            nodes_temp.open("w", encoding="utf-8") as node_writer,
            parents_temp.open("w", encoding="utf-8") as parent_writer,
        ):
            for node in ordered_nodes:
                _raise_if_cancelled(should_cancel)
                if not node.content.strip():
                    continue
                rel_path = _node_source_path(node)
                filename = PurePosixPath(rel_path).name
                stats = document_stats.setdefault(
                    node.document_id,
                    _new_document_stats(
                        node,
                        rel_path,
                        source_records.get(rel_path.casefold()),
                    ),
                )
                stats["node_count"] += 1
                stats["_parser_versions"].add(node.parser_version)
                stats["_node_types"].add(node.node_type.value)
                # 元数据契约：任意节点携带的 document 级统计都收集（不只在首节点）。
                node_doc_stats = (node.metadata or {}).get(
                    "document_statistics"
                ) or {}
                if node_doc_stats.get("page_count") is not None:
                    stats["page_count"] = _as_optional_int(
                        node_doc_stats.get("page_count")
                    )
                if (node.metadata or {}).get("sheet_count") is not None:
                    stats["sheet_count"] = _as_optional_int(
                        (node.metadata or {}).get("sheet_count")
                    )
                parser_versions.add(node.parser_version)
                node_types.add(node.node_type.value)
                node_writer.write(
                    json.dumps(node.to_record(), ensure_ascii=False) + "\n"
                )

                parent_id = node.parent_id or node.node_id
                if node.document_id != active_document_id:
                    written_parents.clear()
                    active_document_id = node.document_id
                if parent_id not in written_parents:
                    parent_writer.write(
                        json.dumps(
                            _parent_record(node, parent_id),
                            ensure_ascii=False,
                        )
                        + "\n"
                    )
                    written_parents.add(parent_id)

                next_chunk_index = chunk_indices.get(node.document_id, 0)
                for node_chunk_index, content in enumerate(
                    split_text(node.content, chunk_size, chunk_overlap)
                ):
                    record = _chunk_record(
                        node,
                        content=content,
                        display_content=(
                            node.display_content
                            if node_chunk_index == 0
                            else None
                        ),
                        parent_id=parent_id,
                        source_label=source_label,
                        source_path=rel_path,
                        filename=filename,
                        chunk_index=next_chunk_index,
                    )
                    chunk_writer.write(
                        json.dumps(record, ensure_ascii=False) + "\n"
                    )
                    index_fingerprint.update(
                        str(record["id"]).encode("utf-8")
                    )
                    index_fingerprint.update(b"\0")
                    index_fingerprint.update(
                        chunk_fingerprint_text(record).encode("utf-8")
                    )
                    index_fingerprint.update(b"\0")
                    chunk_count += 1
                    stats["chunk_count"] += 1
                    stats["content_chars"] += len(content)
                    next_chunk_index += 1
                chunk_indices[node.document_id] = next_chunk_index

        serialized_stats = _serialize_document_stats(document_stats.values())

        manifest = {
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "index_format_version": INDEX_FORMAT_VERSION,
            "document_parser_interface_version": DOCUMENT_PARSER_INTERFACE_VERSION,
            "source_dir": source_label,
            "index_dir": output_dir.as_posix(),
            "file_count": len(serialized_stats),
            "chunk_count": chunk_count,
            "extensions": sorted(SUPPORTED_EXTENSIONS),
            "chunk_size": chunk_size,
            "chunk_overlap": chunk_overlap,
            "index_fingerprint": index_fingerprint.hexdigest(),
            "document_node_schema_version": DOCUMENT_NODE_SCHEMA_VERSION,
            "parser_versions": sorted(parser_versions),
            "node_types": sorted(node_types),
            "nodes_file": NODES_FILE,
            "parents_file": PARENTS_FILE,
            "chunks_file": "chunks.jsonl",
            STORAGE_LAYOUT_FIELD: MONOLITHIC_STORAGE_LAYOUT,
            "documents": serialized_stats,
        }
        manifest_temp.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        _raise_if_cancelled(should_cancel)
        _commit_index_files(
            (
                (nodes_temp, output_dir / NODES_FILE),
                (parents_temp, output_dir / PARENTS_FILE),
                (chunks_temp, output_dir / "chunks.jsonl"),
                (manifest_temp, output_dir / INDEX_MANIFEST_FILE),
            )
        )
        _remove_path(output_dir / SHARDS_DIR)
    except Exception:
        _remove_files(temporary_paths)
        raise

    return LiteIndexStats(
        source_dir=source_label,
        index_dir=output_dir.as_posix(),
        file_count=len(serialized_stats),
        chunk_count=chunk_count,
        added_count=len(serialized_stats),
        documents=serialized_stats,
    )


def append_index(
    documents: Iterable[tuple[str, str]],
    *,
    source_label: str,
    index_dir: str | Path,
    chunk_size: int,
    chunk_overlap: int,
) -> LiteIndexStats:
    return append_node_index(
        _legacy_documents_to_nodes(documents),
        source_label=source_label,
        index_dir=index_dir,
        chunk_size=chunk_size,
        chunk_overlap=chunk_overlap,
    )


def append_node_index(
    nodes: Iterable[DocumentNode],
    *,
    source_label: str,
    index_dir: str | Path,
    chunk_size: int,
    chunk_overlap: int,
) -> LiteIndexStats:
    output_dir = Path(index_dir).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    if (output_dir / INDEX_MANIFEST_FILE).exists():
        try:
            ensure_index_format(output_dir)
        except IndexFormatError:
            _archive_incompatible_index(output_dir)
            output_dir.mkdir(parents=True, exist_ok=True)
    existing_documents = list_index_documents(output_dir)
    if any(
        document.get(SHARD_PATH_FIELD)
        for document in existing_documents
    ):
        return _append_sharded_node_index(
            nodes,
            existing_documents=existing_documents,
            source_label=source_label,
            output_dir=output_dir,
            chunk_size=chunk_size,
            chunk_overlap=chunk_overlap,
        )
    existing_filenames = {doc["filename"].casefold() for doc in existing_documents}

    skipped_files: list[str] = []
    added_documents: set[str] = set()
    skipped_documents: set[str] = set()

    def iter_new_nodes() -> Iterable[DocumentNode]:
        for node in nodes:
            filename = PurePosixPath(_node_source_path(node)).name
            if node.document_id not in added_documents | skipped_documents:
                if filename.casefold() in existing_filenames:
                    skipped_files.append(filename)
                    skipped_documents.add(node.document_id)
                    continue
                existing_filenames.add(filename.casefold())
                added_documents.add(node.document_id)
            if node.document_id in added_documents:
                yield node

    stats = write_node_index(
        chain(iter_nodes(output_dir), iter_new_nodes()),
        source_label=source_label,
        index_dir=output_dir,
        chunk_size=chunk_size,
        chunk_overlap=chunk_overlap,
        document_sources=_document_source_records(existing_documents),
    )
    stats.added_count = len(added_documents)
    stats.skipped_count = len(skipped_files)
    stats.skipped_files = skipped_files
    return stats


def _append_sharded_node_index(
    nodes: Iterable[DocumentNode],
    *,
    existing_documents: list[dict],
    source_label: str,
    output_dir: Path,
    chunk_size: int,
    chunk_overlap: int,
) -> LiteIndexStats:
    existing_filenames = {
        str(document.get("filename") or "").casefold()
        for document in existing_documents
    }
    staged_documents: list[dict[str, Any]] = []
    staged_paths: list[Path] = []
    skipped_files: list[str] = []
    try:
        for document_nodes in _group_nodes_by_document(nodes):
            first = document_nodes[0]
            source_path = _node_source_path(first)
            filename = PurePosixPath(source_path).name
            if filename.casefold() in existing_filenames:
                skipped_files.append(filename)
                continue
            existing_filenames.add(filename.casefold())
            stage_path = output_dir / f".{uuid4().hex}.nodes.jsonl.tmp"
            staged_paths.append(stage_path)
            with stage_path.open("w", encoding="utf-8") as writer:
                for node in document_nodes:
                    writer.write(
                        json.dumps(node.to_record(), ensure_ascii=False) + "\n"
                    )
            staged_documents.append(
                {
                    "document_id": first.document_id,
                    "source_path": source_path,
                    "source_key": source_path.casefold(),
                    "stage_path": stage_path,
                    "source_record": {
                        "filename": filename,
                        "source_path": source_path,
                    },
                }
            )
        if not staged_documents:
            return LiteIndexStats(
                source_dir=source_label,
                index_dir=output_dir.as_posix(),
                file_count=len(existing_documents),
                chunk_count=sum(
                    int(document.get("chunk_count") or 0)
                    for document in existing_documents
                ),
                skipped_count=len(skipped_files),
                documents=existing_documents,
                skipped_files=skipped_files,
            )
        stats = _commit_sharded_sync(
            existing_documents=existing_documents,
            staged_documents=staged_documents,
            removed_source_keys=set(),
            source_label=source_label,
            output_dir=output_dir,
            chunk_size=chunk_size,
            chunk_overlap=chunk_overlap,
            should_cancel=None,
        )
        stats.added_count = len(staged_documents)
        stats.skipped_count = len(skipped_files)
        stats.skipped_files = skipped_files
        return stats
    finally:
        _remove_files(staged_paths)


def delete_index_document(
    filename: str,
    index_dir: str | Path = DEFAULT_INDEX_DIR,
    chunk_size: int = 900,
    chunk_overlap: int = 120,
) -> LiteIndexStats:
    output_dir = Path(index_dir).expanduser().resolve()
    ensure_index_format(output_dir)
    target = PurePosixPath(normalize_upload_name(filename)).name.casefold()
    documents = list_index_documents(output_dir)
    if not any(
        str(document.get("filename") or "").casefold() == target
        for document in documents
    ):
        raise FileNotFoundError(f"Document not found in lite index: {filename}")
    manifest_path = output_dir / INDEX_MANIFEST_FILE
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if any(document.get(SHARD_PATH_FIELD) for document in documents):
            return _delete_sharded_index_document(
                output_dir,
                manifest,
                documents,
                target,
            )

    stats = write_node_index(
        (
            node
            for node in iter_nodes(output_dir)
            if PurePosixPath(_node_source_path(node)).name.casefold() != target
        ),
        source_label="browser_upload",
        index_dir=output_dir,
        chunk_size=chunk_size,
        chunk_overlap=chunk_overlap,
        document_sources={
            key: value
            for key, value in _document_source_records(documents).items()
            if PurePosixPath(key).name.casefold() != target
        },
    )
    stats.added_count = 0
    stats.removed_count = 1
    return stats


def _delete_sharded_index_document(
    output_dir: Path,
    manifest: dict[str, Any],
    documents: list[dict],
    target_filename: str,
) -> LiteIndexStats:
    remaining = [
        dict(document)
        for document in documents
        if str(document.get("filename") or "").casefold() != target_filename
    ]
    fingerprint = hashlib.sha256()
    for chunk in _iter_records_for_documents(
        output_dir,
        remaining,
        "chunks.jsonl",
        "Chunk",
    ):
        fingerprint.update(str(chunk.get("id") or "").encode("utf-8"))
        fingerprint.update(b"\0")
        fingerprint.update(chunk_fingerprint_text(chunk).encode("utf-8"))
        fingerprint.update(b"\0")
    updated_manifest = dict(manifest)
    updated_manifest.update(
        {
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "file_count": len(remaining),
            "chunk_count": sum(
                int(document.get("chunk_count") or 0)
                for document in remaining
            ),
            "index_fingerprint": fingerprint.hexdigest(),
            STORAGE_LAYOUT_FIELD: SHARDED_STORAGE_LAYOUT,
            "documents": remaining,
        }
    )
    manifest_temp = output_dir / f"{INDEX_MANIFEST_FILE}.tmp"
    manifest_temp.write_text(
        json.dumps(updated_manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    _commit_index_files(
        ((manifest_temp, output_dir / INDEX_MANIFEST_FILE),)
    )
    _cleanup_unreferenced_shards(output_dir, remaining)
    if not any(
        not document.get(SHARD_PATH_FIELD)
        for document in remaining
    ):
        _write_empty_compatibility_files(output_dir)
    return LiteIndexStats(
        source_dir=str(updated_manifest.get("source_dir") or ""),
        index_dir=output_dir.as_posix(),
        file_count=len(remaining),
        chunk_count=int(updated_manifest["chunk_count"]),
        removed_count=1,
        documents=remaining,
    )


def iter_chunks(
    index_dir: str | Path = DEFAULT_INDEX_DIR,
) -> Iterator[dict]:
    index_path = Path(index_dir).expanduser().resolve()
    yield from _iter_index_records(index_path, "chunks.jsonl", "Chunk")


def read_chunks(index_dir: str | Path = DEFAULT_INDEX_DIR) -> list[dict]:
    return list(iter_chunks(index_dir))


def iter_nodes(
    index_dir: str | Path = DEFAULT_INDEX_DIR,
) -> Iterator[DocumentNode]:
    index_path = Path(index_dir).expanduser().resolve()
    for record in _iter_index_records(
        index_path,
        NODES_FILE,
        "DocumentNode",
    ):
        try:
            yield DocumentNode.from_record(record)
        except (KeyError, TypeError, ValueError) as exc:
            raise IndexFormatError(
                f"节点索引内容损坏：{index_path}。请重建索引。"
            ) from exc


def read_nodes(index_dir: str | Path = DEFAULT_INDEX_DIR) -> list[DocumentNode]:
    return list(iter_nodes(index_dir))


def iter_parents(
    index_dir: str | Path = DEFAULT_INDEX_DIR,
) -> Iterator[dict]:
    index_path = Path(index_dir).expanduser().resolve()
    yield from _iter_index_records(index_path, PARENTS_FILE, "Parent")


def read_parents(index_dir: str | Path = DEFAULT_INDEX_DIR) -> list[dict]:
    return list(iter_parents(index_dir))


def load_parent_content(
    index_dir: str | Path,
    parent_id: str,
) -> Optional[dict]:
    nodes = {node.node_id: node for node in read_nodes(index_dir)}
    parents = {
        str(item.get("parent_id")): item
        for item in read_parents(index_dir)
    }
    parent = parents.get(str(parent_id))
    if not parent:
        return None
    node = nodes.get(str(parent.get("content_node_id") or ""))
    if not node:
        return None
    return {
        "parent_id": parent_id,
        "document_id": node.document_id,
        "content": node.content,
        "display_content": node.display_content,
        "source_anchor": dict(node.source_anchor),
        "metadata": dict(node.metadata),
    }


def ensure_index_format(index_dir: str | Path) -> None:
    index_path = Path(index_dir).expanduser().resolve()
    recover_index_transaction(index_path)
    manifest_path = index_path / INDEX_MANIFEST_FILE
    if not manifest_path.exists():
        return
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        version = int(manifest.get("index_format_version") or 0)
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        raise IndexFormatError(
            f"索引清单损坏：{manifest_path}。请重建索引。"
        ) from exc
    if version != INDEX_FORMAT_VERSION:
        raise IndexFormatError(
            f"索引格式版本为 {version}，当前需要 {INDEX_FORMAT_VERSION}。"
            " 请重建索引。"
        )
    parser_versions = {
        str(value)
        for value in (manifest.get("parser_versions") or [])
    }
    expected_parser_versions = {
        "pdf_parser_": ("PDF", PDF_PARSER_VERSION),
        "csv_parser_": ("CSV", CSV_PARSER_VERSION),
        "xlsx_parser_": ("XLSX", XLSX_PARSER_VERSION),
    }
    outdated_versions = {
        (label, value)
        for value in parser_versions
        for prefix, (label, current) in expected_parser_versions.items()
        if value.startswith(prefix) and value != current
    }
    if outdated_versions:
        labels = "/".join(
            sorted({label for label, _value in outdated_versions})
        )
        versions = ", ".join(
            sorted(value for _label, value in outdated_versions)
        )
        raise IndexFormatError(
            f"{labels} 解析器版本已升级：索引包含 {versions}。请重建索引。"
        )
    documents = (
        manifest.get("documents")
        if isinstance(manifest.get("documents"), list)
        else []
    )
    sharded_documents = [
        document
        for document in documents
        if document.get(SHARD_PATH_FIELD)
    ]
    legacy_documents = [
        document
        for document in documents
        if not document.get(SHARD_PATH_FIELD)
    ]
    missing = []
    if sharded_documents:
        for document in sharded_documents:
            shard_path = _resolve_shard_path(
                index_path,
                str(document.get(SHARD_PATH_FIELD) or ""),
            )
            for name in (NODES_FILE, PARENTS_FILE, "chunks.jsonl"):
                if not (shard_path / name).exists():
                    missing.append(f"{document.get('filename')}/{name}")
    if not sharded_documents or legacy_documents:
        missing.extend(
            name
            for name in (NODES_FILE, PARENTS_FILE, "chunks.jsonl")
            if not (index_path / name).exists()
        )
    if missing:
        raise IndexFormatError(
            "索引缺少文件：{0}。请重建索引。".format(", ".join(missing))
        )


def write_chunks(index_dir: str | Path, chunks: list[dict]) -> None:
    output_dir = Path(index_dir).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    chunks_path = output_dir / "chunks.jsonl"
    with chunks_path.open("w", encoding="utf-8") as writer:
        for chunk in chunks:
            writer.write(json.dumps(chunk, ensure_ascii=False) + "\n")


def list_index_documents(index_dir: str | Path = DEFAULT_INDEX_DIR, chunks: list[dict] | None = None) -> list[dict]:
    index_path = Path(index_dir).expanduser().resolve()
    recover_index_transaction(index_path)
    manifest_path = index_path / "manifest.json"
    if manifest_path.exists():
        ensure_index_format(index_path)
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise IndexFormatError(
                f"索引清单损坏：{manifest_path}。请重建索引。"
            ) from exc
        documents = manifest.get("documents")
        if isinstance(documents, list):
            return documents
    return summarize_documents(read_chunks(index_path) if chunks is None else chunks)


def summarize_documents(chunks: list[dict]) -> list[dict]:
    documents: dict[str, dict] = {}
    for chunk in chunks:
        filename = str(chunk.get("filename") or PurePosixPath(str(chunk.get("source_path") or "")).name)
        source_path = str(chunk.get("source_path") or filename)
        key = filename.casefold()
        item = documents.setdefault(key, {
            "document_id": str(
                chunk.get("document_id") or document_id_from_source(source_path)
            ),
            "filename": filename,
            "source_path": source_path,
            "chunk_count": 0,
            "content_chars": 0,
            "_node_ids": set(),
            "_parser_versions": set(),
            "_node_types": set(),
        })
        item["chunk_count"] += 1
        item["content_chars"] += int(chunk.get("content_chars") or len(str(chunk.get("content", ""))))
        if chunk.get("node_id"):
            item["_node_ids"].add(str(chunk["node_id"]))
        if chunk.get("parser_version"):
            item["_parser_versions"].add(str(chunk["parser_version"]))
        if chunk.get("node_type"):
            item["_node_types"].add(str(chunk["node_type"]))
    return sorted(
        _serialize_document_stats(documents.values()),
        key=lambda item: item["filename"].casefold(),
    )


def iter_supported_files(source_dir: Path) -> Iterable[Path]:
    for path in sorted(source_dir.rglob("*")):
        if path.is_file() and path.suffix.lower() in SUPPORTED_EXTENSIONS:
            yield path


def extract_document_nodes(
    file_path: Path,
    *,
    source_path: str | None = None,
) -> list[DocumentNode]:
    file_path = Path(file_path)
    return list(
        iter_document_nodes(
            file_path,
            source_path=source_path or file_path.name,
        )
    )


def extract_document_nodes_from_bytes(
    filename: str,
    content: bytes,
) -> list[DocumentNode]:
    return list(iter_document_nodes_from_bytes(filename, content))


def extract_text(file_path: Path) -> str:
    return "\n".join(node.content for node in extract_document_nodes(file_path))


def extract_text_from_bytes(filename: str, content: bytes) -> str:
    return "\n".join(
        node.content
        for node in extract_document_nodes_from_bytes(filename, content)
    )


def _legacy_documents_to_nodes(
    documents: Iterable[tuple[str, str]],
) -> Iterable[DocumentNode]:
    for name, text in documents:
        rel_path = normalize_upload_name(name)
        if not str(text).strip():
            continue
        yield DocumentNode(
            document_id=document_id_from_source(rel_path),
            content=str(text),
            parser_version=LITE_PARSER_VERSION,
            node_type=NodeType.TEXT,
            sequence=0,
            source_anchor={"source_path": rel_path},
            metadata={
                "filename": PurePosixPath(rel_path).name,
                "file_type": PurePosixPath(rel_path).suffix.lower(),
            },
        )


def _group_nodes_by_document(
    nodes: Iterable[DocumentNode],
) -> list[list[DocumentNode]]:
    grouped: dict[str, list[DocumentNode]] = {}
    for node in nodes:
        if not node.content.strip():
            continue
        grouped.setdefault(node.document_id, []).append(node)
    return [
        sorted(items, key=lambda item: item.sequence)
        for items in grouped.values()
        if items
    ]


def _ordered_nodes(
    nodes: Iterable[DocumentNode],
) -> Iterable[DocumentNode]:
    if isinstance(nodes, (list, tuple)):
        for document_nodes in _group_nodes_by_document(nodes):
            yield from document_nodes
        return
    for node in nodes:
        if node.content.strip():
            yield node


def _node_source_path(node: DocumentNode) -> str:
    value = (
        node.source_anchor.get("source_path")
        or node.metadata.get("source_path")
        or node.metadata.get("filename")
        or node.document_id
    )
    return normalize_upload_name(str(value))


def _relative_source_path(path: Path, source_root: Path | None) -> str:
    if source_root is not None:
        try:
            return normalize_upload_name(path.relative_to(source_root).as_posix())
        except ValueError:
            pass
    return normalize_upload_name(path.name)


def _source_record_for_path(
    path: Path,
    source_path: str,
    *,
    should_cancel: CancelCheck | None = None,
) -> dict[str, Any]:
    digest = hashlib.sha256()
    byte_count = 0
    with path.open("rb") as reader:
        while True:
            _raise_if_cancelled(should_cancel)
            chunk = reader.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            byte_count += len(chunk)
    stat = path.stat()
    if byte_count != stat.st_size:
        raise OSError(f"Document changed while fingerprinting: {path}")
    return {
        "filename": path.name,
        "source_path": source_path,
        SOURCE_SHA256_FIELD: digest.hexdigest(),
        SOURCE_SIZE_FIELD: int(stat.st_size),
        SOURCE_MTIME_NS_FIELD: int(stat.st_mtime_ns),
        # 原文件绝对路径（仅本地 manifest，不进远程文本）
        "origin_path": str(path.resolve()),
    }


def _document_source_records(
    documents: Iterable[Mapping[str, Any]],
) -> dict[str, dict[str, Any]]:
    records: dict[str, dict[str, Any]] = {}
    for document in documents:
        source_path = normalize_upload_name(
            str(document.get("source_path") or document.get("filename") or "")
        )
        record = {
            "filename": str(
                document.get("filename") or PurePosixPath(source_path).name
            ),
            "source_path": source_path,
        }
        for field_name in (
            SOURCE_SHA256_FIELD,
            SOURCE_SIZE_FIELD,
            SOURCE_MTIME_NS_FIELD,
        ):
            if document.get(field_name) is not None:
                record[field_name] = document[field_name]
        if document.get("origin_path"):
            record["origin_path"] = str(document["origin_path"])
        records[source_path.casefold()] = record
    return records


def _raise_if_cancelled(should_cancel: CancelCheck | None) -> None:
    if should_cancel is not None and should_cancel():
        raise IndexCancelledError("Indexing was cancelled.")


def _emit_progress(
    callback: ProgressCallback | None,
    **payload: Any,
) -> None:
    if callback is not None:
        callback(dict(payload))


def _chunk_record(
    node: DocumentNode,
    *,
    content: str,
    display_content: str | None,
    parent_id: str,
    source_label: str,
    source_path: str,
    filename: str,
    chunk_index: int,
) -> dict:
    source_anchor = dict(node.source_anchor)
    source_anchor.setdefault("source_path", source_path)
    return {
        "id": f"{source_path}:{chunk_index}",
        "schema_version": node.schema_version,
        "source_dir": source_label,
        "source_path": source_path,
        "filename": filename,
        "chunk_index": chunk_index,
        "content": content,
        "display_content": display_content,
        "content_chars": len(content),
        "content_hash": content_sha256(content),
        "node_id": node.node_id,
        "document_id": node.document_id,
        "node_content_hash": node.content_hash,
        "parser_version": node.parser_version,
        "node_type": node.node_type.value,
        "page_or_sheet": node.page_or_sheet,
        "section_path": list(node.section_path),
        "sequence": node.sequence,
        "bbox": node.bbox.to_list() if node.bbox else None,
        "row_start": node.row_start,
        "row_end": node.row_end,
        "column_start": node.column_start,
        "column_end": node.column_end,
        "parent_id": parent_id,
        "source_anchor": source_anchor,
        "metadata": dict(node.metadata),
    }


def _search_text(
    *,
    content: str,
    filename: str,
    source_path: str,
    node_type: str,
    page_or_sheet: Any,
    metadata: Any,
) -> str:
    resolved_filename = filename or PurePosixPath(source_path).name
    suffix = PurePosixPath(resolved_filename).suffix.lower()
    file_label = {
        ".xlsx": "Excel XLSX 工作簿 电子表格",
        ".csv": "CSV 表格",
        ".pdf": "PDF 文档",
        ".md": "Markdown 文档",
        ".txt": "文本文件",
    }.get(suffix, suffix.lstrip("."))
    node_label = {
        NodeType.WORKBOOK_SUMMARY.value: "工作簿摘要",
        NodeType.SHEET_SUMMARY.value: "Sheet 摘要",
        NodeType.ROW_GROUP.value: "表格行数据",
        NodeType.TABLE.value: "表格",
        NodeType.FIGURE.value: "图片",
        NodeType.TEXT.value: "正文",
    }.get(str(node_type), str(node_type))
    lines = [
        f"文件名：{resolved_filename}",
        f"文件主名：{PurePosixPath(resolved_filename).stem}",
    ]
    if file_label:
        lines.append(f"文件类型：{file_label}")
    if page_or_sheet is not None:
        lines.append(f"Sheet或页面：{page_or_sheet}")
    if node_label:
        lines.append(f"内容类型：{node_label}")
    resolved_metadata = metadata if isinstance(metadata, Mapping) else {}
    columns = resolved_metadata.get("columns")
    if isinstance(columns, list) and columns:
        lines.append("字段：" + "、".join(str(column) for column in columns))
    lines.append(str(content or ""))
    return "\n".join(line for line in lines if line.strip())


def _parent_record(node: DocumentNode, parent_id: str) -> dict:
    return {
        "schema_version": node.schema_version,
        "parent_id": parent_id,
        "document_id": node.document_id,
        "content_node_id": node.node_id,
        "parser_version": node.parser_version,
        "node_type": node.node_type.value,
        "page_or_sheet": node.page_or_sheet,
        "section_path": list(node.section_path),
        "source_anchor": dict(node.source_anchor),
        "metadata": dict(node.metadata),
    }


def _replace_index_file(source: Path, target: Path) -> None:
    source.replace(target)


def recover_index_transaction(index_dir: str | Path) -> bool:
    output_dir = Path(index_dir).expanduser().resolve()
    journal_path = output_dir / INDEX_TRANSACTION_FILE
    journal_temp = journal_path.with_suffix(".tmp")
    if not journal_path.exists():
        journal_temp.unlink(missing_ok=True)
        return False
    try:
        journal = json.loads(journal_path.read_text(encoding="utf-8"))
        entries = list(journal.get("entries") or [])
        if not entries:
            raise ValueError("transaction journal has no entries")
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        raise IndexFormatError(
            f"索引事务日志损坏：{journal_path}。请手动重建索引。"
        ) from exc

    source_paths = [Path(str(entry["source"])) for entry in entries]
    commit_completed = (
        journal.get("phase") == "committed"
        or not any(path.exists() for path in source_paths)
    )
    if commit_completed:
        for entry in entries:
            if entry.get("backup"):
                _remove_path(Path(str(entry["backup"])))
        for source_path in source_paths:
            _remove_path(source_path)
        journal_path.unlink(missing_ok=True)
        journal_temp.unlink(missing_ok=True)
        return True

    _rollback_index_transaction(entries)
    journal_path.unlink(missing_ok=True)
    journal_temp.unlink(missing_ok=True)
    return True


def _commit_index_files(files: Iterable[tuple[Path, Path]]) -> None:
    token = uuid4().hex
    file_pairs = list(files)
    if not file_pairs:
        return
    output_dir = Path(
        os.path.commonpath(
            [str(target.parent.resolve()) for _source, target in file_pairs]
        )
    )
    recover_index_transaction(output_dir)
    journal_path = output_dir / INDEX_TRANSACTION_FILE
    journal_temp = journal_path.with_suffix(".tmp")
    entries = []
    for source, target in file_pairs:
        backup = target.with_name(f".{target.name}.{token}.bak")
        entries.append(
            {
                "source": str(source.resolve()),
                "target": str(target.resolve()),
                "backup": str(backup.resolve()),
                "had_target": target.exists(),
            }
        )
    journal = {
        "version": 1,
        "token": token,
        "phase": "installing",
        "entries": entries,
    }
    journal_temp.write_text(
        json.dumps(journal, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    journal_temp.replace(journal_path)
    try:
        for entry in entries:
            source = Path(entry["source"])
            target = Path(entry["target"])
            backup = Path(entry["backup"])
            if entry["had_target"]:
                target.replace(backup)
            _replace_index_file(source, target)
        journal["phase"] = "committed"
        journal_temp.write_text(
            json.dumps(journal, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        journal_temp.replace(journal_path)
    except Exception:
        _rollback_index_transaction(entries)
        journal_path.unlink(missing_ok=True)
        journal_temp.unlink(missing_ok=True)
        raise
    for entry in entries:
        _remove_path(Path(entry["backup"]))
    journal_path.unlink(missing_ok=True)
    journal_temp.unlink(missing_ok=True)


def _rollback_index_transaction(entries: Iterable[Mapping[str, Any]]) -> None:
    entry_list = list(entries)
    for entry in reversed(entry_list):
        source = Path(str(entry["source"]))
        target = Path(str(entry["target"]))
        backup = Path(str(entry["backup"]))
        if backup.exists():
            _remove_path(target)
            backup.replace(target)
        elif not bool(entry.get("had_target")):
            _remove_path(target)
        _remove_path(source)


def _remove_files(paths: Iterable[Path]) -> None:
    for path in paths:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass


def _remove_path(path: Path) -> None:
    try:
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink(missing_ok=True)
    except OSError:
        pass


def _archive_incompatible_index(index_path: Path) -> Path:
    if not index_path.exists():
        return index_path
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    candidate = index_path.with_name(
        f"{index_path.name}.old_incompatible_{timestamp}"
    )
    sequence = 1
    while candidate.exists():
        candidate = index_path.with_name(
            f"{index_path.name}.old_incompatible_{timestamp}_{sequence}"
        )
        sequence += 1
    index_path.replace(candidate)
    return candidate


def _iter_index_records(
    index_path: Path,
    filename: str,
    label: str,
) -> Iterator[dict]:
    manifest_path = index_path / INDEX_MANIFEST_FILE
    if not manifest_path.exists():
        yield from _iter_jsonl_records(index_path / filename, label)
        return
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise IndexFormatError(
            f"索引清单损坏：{manifest_path}。请重建索引。"
        ) from exc
    documents = (
        manifest.get("documents")
        if isinstance(manifest.get("documents"), list)
        else []
    )
    sharded_documents = [
        document
        for document in documents
        if document.get(SHARD_PATH_FIELD)
    ]
    if not sharded_documents:
        yield from _iter_jsonl_records(index_path / filename, label)
        return

    legacy_document_ids = {
        str(document.get("document_id") or "")
        for document in documents
        if not document.get(SHARD_PATH_FIELD)
    }
    if legacy_document_ids:
        for record in _iter_jsonl_records(index_path / filename, label):
            if str(record.get("document_id") or "") in legacy_document_ids:
                yield record
    for document in sharded_documents:
        shard_path = _resolve_shard_path(
            index_path,
            str(document.get(SHARD_PATH_FIELD) or ""),
        )
        yield from _iter_jsonl_records(shard_path / filename, label)


def _resolve_shard_path(index_path: Path, relative_path: str) -> Path:
    candidate = (index_path / normalize_upload_name(relative_path)).resolve()
    try:
        candidate.relative_to(index_path)
    except ValueError as exc:
        raise IndexFormatError(
            f"索引分片路径越界：{relative_path}。请重建索引。"
        ) from exc
    return candidate


def _iter_jsonl_records(path: Path, label: str) -> Iterator[dict]:
    if not path.exists():
        return
    try:
        with path.open("r", encoding="utf-8") as reader:
            for line_number, line in enumerate(reader, 1):
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise IndexFormatError(
                        f"{label} 索引损坏：{path} 第 {line_number} 行。请重建索引。"
                    ) from exc
                if not isinstance(record, dict):
                    raise IndexFormatError(
                        f"{label} 索引损坏：{path} 第 {line_number} 行不是对象。"
                        " 请重建索引。"
                    )
                yield record
    except OSError as exc:
        raise IndexFormatError(
            f"无法读取 {label} 索引：{path}。请重建索引。"
        ) from exc


def _new_document_stats(
    node: DocumentNode,
    source_path: str,
    source_record: Mapping[str, Any] | None = None,
) -> dict:
    # 元数据契约：解析器在第一个节点透传 document 级统计。
    doc_statistics = (node.metadata or {}).get("document_statistics") or {}
    stats = {
        "document_id": node.document_id,
        "filename": PurePosixPath(source_path).name,
        "source_path": source_path,
        "node_count": 0,
        "chunk_count": 0,
        "content_chars": 0,
        "page_count": _as_optional_int(doc_statistics.get("page_count")),
        # XLSX workbook_summary 的 metadata.sheet_count 已由解析器写入
        "sheet_count": _as_optional_int(
            (node.metadata or {}).get("sheet_count")
            or doc_statistics.get("sheet_count")
        ),
        "origin_path": str((source_record or {}).get("origin_path") or ""),
        "_parser_versions": set(),
        "_node_types": set(),
    }
    for field_name in (
        SOURCE_SHA256_FIELD,
        SOURCE_SIZE_FIELD,
        SOURCE_MTIME_NS_FIELD,
    ):
        if source_record and source_record.get(field_name) is not None:
            stats[field_name] = source_record[field_name]
    return stats


def _as_optional_int(value: Any) -> Optional[int]:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _serialize_document_stats(values: Iterable[dict]) -> list[dict]:
    serialized = []
    for value in values:
        item = dict(value)
        node_ids = set(item.pop("_node_ids", set()))
        parser_versions = set(item.pop("_parser_versions", set()))
        node_types = set(item.pop("_node_types", set()))
        if node_ids:
            item["node_count"] = len(node_ids)
        else:
            item["node_count"] = int(item.get("node_count") or 0)
        item["parser_versions"] = sorted(parser_versions)
        item["node_types"] = sorted(node_types)
        serialized.append(item)
    return serialized


def normalize_upload_name(name: str) -> str:
    raw_parts = PurePosixPath(str(name).replace("\\", "/")).parts
    safe_parts = [part for part in raw_parts if part not in ("", ".", "..")]
    if not safe_parts:
        return "untitled"
    return PurePosixPath(*safe_parts).as_posix()


def split_text(text: str, chunk_size: int, chunk_overlap: int) -> list[str]:
    normalized = "\n".join(line.strip() for line in str(text).splitlines())
    paragraphs = [part.strip() for part in normalized.split("\n\n") if part.strip()]
    chunks = []
    current = ""

    for paragraph in paragraphs:
        if len(paragraph) > chunk_size:
            if current:
                chunks.append(current.strip())
                current = ""
            chunks.extend(_split_long_text(paragraph, chunk_size, chunk_overlap))
            continue

        next_text = f"{current}\n\n{paragraph}".strip() if current else paragraph
        if len(next_text) <= chunk_size:
            current = next_text
        else:
            chunks.append(current.strip())
            current = paragraph

    if current:
        chunks.append(current.strip())
    return chunks


def _split_long_text(text: str, chunk_size: int, chunk_overlap: int) -> list[str]:
    chunks = []
    start = 0
    while start < len(text):
        end = min(start + chunk_size, len(text))
        chunks.append(text[start:end].strip())
        if end == len(text):
            break
        start = max(end - chunk_overlap, start + 1)
    return chunks
