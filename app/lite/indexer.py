from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Iterable


SUPPORTED_EXTENSIONS = {".md", ".txt", ".pdf"}
DEFAULT_INDEX_DIR = Path("data/lite_index")


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


def build_index(
    source_dir: str | Path,
    index_dir: str | Path = DEFAULT_INDEX_DIR,
    chunk_size: int = 900,
    chunk_overlap: int = 120,
) -> LiteIndexStats:
    source_path = Path(source_dir).expanduser().resolve()
    if not source_path.exists() or not source_path.is_dir():
        raise FileNotFoundError(f"Source directory does not exist: {source_path}")

    documents = []
    for file_path in iter_supported_files(source_path):
        text = extract_text(file_path)
        if text.strip():
            documents.append((file_path.relative_to(source_path).as_posix(), text))

    return write_index(
        documents,
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
    return append_index(
        documents,
        source_label="browser_upload",
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
    output_dir = Path(index_dir).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    chunks_path = output_dir / "chunks.jsonl"
    manifest_path = output_dir / "manifest.json"

    file_count = 0
    chunk_count = 0
    document_stats = []
    with chunks_path.open("w", encoding="utf-8") as writer:
        for name, text in documents:
            if not text.strip():
                continue
            file_count += 1
            rel_path = normalize_upload_name(name)
            current_chunk_count = 0
            current_content_chars = 0
            for chunk_index, content in enumerate(split_text(text, chunk_size, chunk_overlap)):
                record = {
                    "id": f"{rel_path}:{chunk_index}",
                    "source_dir": source_label,
                    "source_path": rel_path,
                    "filename": PurePosixPath(rel_path).name,
                    "chunk_index": chunk_index,
                    "content": content,
                    "content_chars": len(content),
                }
                writer.write(json.dumps(record, ensure_ascii=False) + "\n")
                chunk_count += 1
                current_chunk_count += 1
                current_content_chars += len(content)
            document_stats.append({
                "filename": PurePosixPath(rel_path).name,
                "source_path": rel_path,
                "chunk_count": current_chunk_count,
                "content_chars": current_content_chars,
            })

    manifest = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "source_dir": source_label,
        "index_dir": output_dir.as_posix(),
        "file_count": file_count,
        "chunk_count": chunk_count,
        "extensions": sorted(SUPPORTED_EXTENSIONS),
        "chunk_size": chunk_size,
        "chunk_overlap": chunk_overlap,
        "documents": document_stats,
    }
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    return LiteIndexStats(
        source_dir=source_label,
        index_dir=output_dir.as_posix(),
        file_count=file_count,
        chunk_count=chunk_count,
        added_count=file_count,
        documents=document_stats,
    )


def append_index(
    documents: Iterable[tuple[str, str]],
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

    for name, text in documents:
        if not text.strip():
            continue
        rel_path = normalize_upload_name(name)
        filename = PurePosixPath(rel_path).name
        if filename.casefold() in existing_filenames:
            skipped_files.append(filename)
            continue

        current_chunks = split_text(text, chunk_size, chunk_overlap)
        for chunk_index, content in enumerate(current_chunks):
            chunks.append({
                "id": f"{rel_path}:{chunk_index}",
                "source_dir": source_label,
                "source_path": rel_path,
                "filename": filename,
                "chunk_index": chunk_index,
                "content": content,
                "content_chars": len(content),
            })
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
            "filename": filename,
            "source_path": source_path,
            "chunk_count": 0,
            "content_chars": 0,
        })
        item["chunk_count"] += 1
        item["content_chars"] += int(chunk.get("content_chars") or len(str(chunk.get("content", ""))))
    return sorted(documents.values(), key=lambda item: item["filename"].casefold())


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
        "documents": documents,
    }
    (output_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")


def iter_supported_files(source_dir: Path) -> Iterable[Path]:
    for path in sorted(source_dir.rglob("*")):
        if path.is_file() and path.suffix.lower() in SUPPORTED_EXTENSIONS:
            yield path


def extract_text(file_path: Path) -> str:
    suffix = file_path.suffix.lower()
    if suffix == ".pdf":
        import fitz

        parts = []
        with fitz.open(file_path) as doc:
            for page in doc:
                parts.append(page.get_text())
        return "\n".join(parts)
    return file_path.read_text(encoding="utf-8", errors="ignore")


def extract_text_from_bytes(filename: str, content: bytes) -> str:
    suffix = PurePosixPath(filename).suffix.lower()
    if suffix == ".pdf":
        import fitz

        parts = []
        with fitz.open(stream=content, filetype="pdf") as doc:
            for page in doc:
                parts.append(page.get_text())
        return "\n".join(parts)
    return content.decode("utf-8", errors="ignore")


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
