from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Iterable

from app.documents import (
    DOCUMENT_NODE_SCHEMA_VERSION,
    DocumentNode,
    NodeType,
    content_sha256,
    document_id_from_source,
)


SUPPORTED_EXTENSIONS = {".md", ".txt", ".pdf"}
DEFAULT_INDEX_DIR = Path("data/lite_index")
LITE_PARSER_VERSION = "lite_legacy_text_v1"
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
    skipped_count: int = 0
    documents: list[dict] = field(default_factory=list)
    skipped_files: list[str] = field(default_factory=list)


def chunk_structure(record: dict) -> dict:
    return {
        key: record.get(key)
        for key in STRUCTURAL_CHUNK_FIELDS
        if key in record
    }


def build_index(
    source_dir: str | Path,
    index_dir: str | Path = DEFAULT_INDEX_DIR,
    chunk_size: int = 900,
    chunk_overlap: int = 120,
) -> LiteIndexStats:
    source_path = Path(source_dir).expanduser().resolve()
    if not source_path.exists() or not source_path.is_dir():
        raise FileNotFoundError(f"Source directory does not exist: {source_path}")

    nodes = []
    for file_path in iter_supported_files(source_path):
        source_path_value = file_path.relative_to(source_path).as_posix()
        nodes.extend(
            extract_document_nodes(
                file_path,
                source_path=source_path_value,
            )
        )

    return write_node_index(
        nodes,
        source_label=source_path.as_posix(),
        index_dir=index_dir,
        chunk_size=chunk_size,
        chunk_overlap=chunk_overlap,
    )


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
) -> LiteIndexStats:
    output_dir = Path(index_dir).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    chunks_path = output_dir / "chunks.jsonl"
    manifest_path = output_dir / "manifest.json"

    chunk_count = 0
    chunk_indices: dict[str, int] = {}
    document_stats: dict[str, dict] = {}
    with chunks_path.open("w", encoding="utf-8") as writer:
        for document_nodes in _group_nodes_by_document(nodes):
            for node in document_nodes:
                rel_path = _node_source_path(node)
                filename = PurePosixPath(rel_path).name
                stats = document_stats.setdefault(
                    node.document_id,
                    _new_document_stats(node, rel_path),
                )
                stats["_node_ids"].add(node.node_id)
                stats["_parser_versions"].add(node.parser_version)
                stats["_node_types"].add(node.node_type.value)
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
                        source_label=source_label,
                        source_path=rel_path,
                        filename=filename,
                        chunk_index=next_chunk_index,
                    )
                    writer.write(json.dumps(record, ensure_ascii=False) + "\n")
                    chunk_count += 1
                    stats["chunk_count"] += 1
                    stats["content_chars"] += len(content)
                    next_chunk_index += 1
                chunk_indices[node.document_id] = next_chunk_index

    serialized_stats = _serialize_document_stats(document_stats.values())

    manifest = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "source_dir": source_label,
        "index_dir": output_dir.as_posix(),
        "file_count": len(serialized_stats),
        "chunk_count": chunk_count,
        "extensions": sorted(SUPPORTED_EXTENSIONS),
        "chunk_size": chunk_size,
        "chunk_overlap": chunk_overlap,
        "document_node_schema_version": DOCUMENT_NODE_SCHEMA_VERSION,
        "documents": serialized_stats,
    }
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

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

    existing_chunks = read_chunks(output_dir)
    existing_documents = list_index_documents(output_dir, existing_chunks)
    existing_filenames = {doc["filename"].casefold() for doc in existing_documents}

    chunks = list(existing_chunks)
    skipped_files = []
    added_count = 0
    grouped_nodes = _group_nodes_by_document(nodes)
    for document_nodes in grouped_nodes:
        first_node = document_nodes[0]
        rel_path = _node_source_path(first_node)
        filename = PurePosixPath(rel_path).name
        if filename.casefold() in existing_filenames:
            skipped_files.append(filename)
            continue

        chunk_index = 0
        for node in document_nodes:
            for node_chunk_index, content in enumerate(
                split_text(node.content, chunk_size, chunk_overlap)
            ):
                chunks.append(
                    _chunk_record(
                        node,
                        content=content,
                        display_content=(
                            node.display_content
                            if node_chunk_index == 0
                            else None
                        ),
                        source_label=source_label,
                        source_path=rel_path,
                        filename=filename,
                        chunk_index=chunk_index,
                    )
                )
                chunk_index += 1
        existing_filenames.add(filename.casefold())
        added_count += 1

    documents_after = summarize_documents(chunks)
    write_chunks(output_dir, chunks)
    write_manifest(
        output_dir,
        source_label=source_label,
        chunk_size=chunk_size,
        chunk_overlap=chunk_overlap,
        documents=documents_after,
    )

    return LiteIndexStats(
        source_dir=source_label,
        index_dir=output_dir.as_posix(),
        file_count=len(documents_after),
        chunk_count=len(chunks),
        added_count=added_count,
        skipped_count=len(skipped_files),
        documents=documents_after,
        skipped_files=skipped_files,
    )


def delete_index_document(
    filename: str,
    index_dir: str | Path = DEFAULT_INDEX_DIR,
    chunk_size: int = 900,
    chunk_overlap: int = 120,
) -> LiteIndexStats:
    output_dir = Path(index_dir).expanduser().resolve()
    chunks = read_chunks(output_dir)
    target = PurePosixPath(normalize_upload_name(filename)).name.casefold()
    kept_chunks = [chunk for chunk in chunks if str(chunk.get("filename", "")).casefold() != target]
    removed_count = len(chunks) - len(kept_chunks)
    if removed_count == 0:
        raise FileNotFoundError(f"Document not found in lite index: {filename}")

    write_chunks(output_dir, kept_chunks)
    documents_after = summarize_documents(kept_chunks)
    write_manifest(
        output_dir,
        source_label="browser_upload",
        chunk_size=chunk_size,
        chunk_overlap=chunk_overlap,
        documents=documents_after,
    )
    return LiteIndexStats(
        source_dir="browser_upload",
        index_dir=output_dir.as_posix(),
        file_count=len(documents_after),
        chunk_count=len(kept_chunks),
        documents=documents_after,
    )


def read_chunks(index_dir: str | Path = DEFAULT_INDEX_DIR) -> list[dict]:
    chunks_path = Path(index_dir).expanduser().resolve() / "chunks.jsonl"
    if not chunks_path.exists():
        return []
    chunks = []
    with chunks_path.open("r", encoding="utf-8") as reader:
        for line in reader:
            if line.strip():
                chunks.append(json.loads(line))
    return chunks


def write_chunks(index_dir: str | Path, chunks: list[dict]) -> None:
    output_dir = Path(index_dir).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    chunks_path = output_dir / "chunks.jsonl"
    with chunks_path.open("w", encoding="utf-8") as writer:
        for chunk in chunks:
            writer.write(json.dumps(chunk, ensure_ascii=False) + "\n")


def list_index_documents(index_dir: str | Path = DEFAULT_INDEX_DIR, chunks: list[dict] | None = None) -> list[dict]:
    index_path = Path(index_dir).expanduser().resolve()
    manifest_path = index_path / "manifest.json"
    if manifest_path.exists():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
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


def write_manifest(
    index_dir: str | Path,
    *,
    source_label: str,
    chunk_size: int,
    chunk_overlap: int,
    documents: list[dict],
) -> None:
    output_dir = Path(index_dir).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    chunk_count = sum(int(doc.get("chunk_count") or 0) for doc in documents)
    manifest = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "source_dir": source_label,
        "index_dir": output_dir.as_posix(),
        "file_count": len(documents),
        "chunk_count": chunk_count,
        "extensions": sorted(SUPPORTED_EXTENSIONS),
        "chunk_size": chunk_size,
        "chunk_overlap": chunk_overlap,
        "document_node_schema_version": DOCUMENT_NODE_SCHEMA_VERSION,
        "documents": documents,
    }
    (output_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")


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
    rel_path = normalize_upload_name(source_path or file_path.name)
    text = _extract_raw_text(file_path)
    if not text.strip():
        return []
    return [
        DocumentNode(
            document_id=document_id_from_source(rel_path),
            content=text,
            parser_version=LITE_PARSER_VERSION,
            node_type=NodeType.TEXT,
            sequence=0,
            source_anchor={"source_path": rel_path},
            metadata={
                "filename": PurePosixPath(rel_path).name,
                "file_type": file_path.suffix.lower(),
            },
        )
    ]


def extract_document_nodes_from_bytes(
    filename: str,
    content: bytes,
) -> list[DocumentNode]:
    rel_path = normalize_upload_name(filename)
    text = _extract_raw_text_from_bytes(rel_path, content)
    if not text.strip():
        return []
    return [
        DocumentNode(
            document_id=document_id_from_source(rel_path),
            content=text,
            parser_version=LITE_PARSER_VERSION,
            node_type=NodeType.TEXT,
            sequence=0,
            source_anchor={"source_path": rel_path},
            metadata={
                "filename": PurePosixPath(rel_path).name,
                "file_type": PurePosixPath(rel_path).suffix.lower(),
            },
        )
    ]


def extract_text(file_path: Path) -> str:
    return "\n".join(node.content for node in extract_document_nodes(file_path))


def extract_text_from_bytes(filename: str, content: bytes) -> str:
    return "\n".join(
        node.content
        for node in extract_document_nodes_from_bytes(filename, content)
    )


def _extract_raw_text(file_path: Path) -> str:
    suffix = file_path.suffix.lower()
    if suffix == ".pdf":
        import fitz

        parts = []
        with fitz.open(file_path) as doc:
            for page in doc:
                parts.append(page.get_text())
        return "\n".join(parts)
    return file_path.read_text(encoding="utf-8", errors="ignore")


def _extract_raw_text_from_bytes(filename: str, content: bytes) -> str:
    suffix = PurePosixPath(filename).suffix.lower()
    if suffix == ".pdf":
        import fitz

        parts = []
        with fitz.open(stream=content, filetype="pdf") as doc:
            for page in doc:
                parts.append(page.get_text())
        return "\n".join(parts)
    return content.decode("utf-8", errors="ignore")


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


def _node_source_path(node: DocumentNode) -> str:
    value = (
        node.source_anchor.get("source_path")
        or node.metadata.get("source_path")
        or node.metadata.get("filename")
        or node.document_id
    )
    return normalize_upload_name(str(value))


def _chunk_record(
    node: DocumentNode,
    *,
    content: str,
    display_content: str | None,
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
        "parent_id": node.parent_id,
        "source_anchor": source_anchor,
        "metadata": dict(node.metadata),
    }


def _new_document_stats(node: DocumentNode, source_path: str) -> dict:
    return {
        "document_id": node.document_id,
        "filename": PurePosixPath(source_path).name,
        "source_path": source_path,
        "node_count": 0,
        "chunk_count": 0,
        "content_chars": 0,
        "_node_ids": set(),
        "_parser_versions": set(),
        "_node_types": set(),
    }


def _serialize_document_stats(values: Iterable[dict]) -> list[dict]:
    serialized = []
    for value in values:
        item = dict(value)
        node_ids = set(item.pop("_node_ids", set()))
        parser_versions = set(item.pop("_parser_versions", set()))
        node_types = set(item.pop("_node_types", set()))
        item["node_count"] = len(node_ids)
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
