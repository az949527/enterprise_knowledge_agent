from __future__ import annotations

from pathlib import Path
import re
import sys
from typing import List, Optional

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app.lite.generator import answer_query
from app.lite.indexer import (
    DEFAULT_INDEX_DIR,
    SUPPORTED_EXTENSIONS,
    build_index,
    build_index_from_nodes,
    delete_index_document,
    extract_document_nodes_from_bytes,
    list_index_documents,
)
from app.lite.search import search_index


class IndexRequest(BaseModel):
    source_dir: str = Field(default="demo_documents")
    index_dir: str = Field(default=str(DEFAULT_INDEX_DIR))


class QueryRequest(BaseModel):
    query: str = Field(..., min_length=1)
    index_dir: str = Field(default=str(DEFAULT_INDEX_DIR))
    top_k: int = Field(default=5, ge=1, le=20)
    use_llm: bool = True
    api_key: Optional[str] = None
    base_url: Optional[str] = None
    model: Optional[str] = None


def resource_path(relative_path: str) -> Path:
    base_dir = Path(getattr(sys, "_MEIPASS", Path.cwd()))
    return base_dir / relative_path


app = FastAPI(title="Local Knowledge Tool Lite")
app.mount("/static", StaticFiles(directory=resource_path("app/lite/static")), name="static")


@app.get("/")
async def home():
    return FileResponse(resource_path("app/lite/static/index.html"))


@app.get("/health")
async def health():
    return {"status": "ok", "mode": "lite"}


@app.get("/api/lite/status")
async def index_status(index_dir: str = str(DEFAULT_INDEX_DIR)):
    import json

    index_path = Path(index_dir).expanduser().resolve()
    manifest_path = index_path / "manifest.json"
    chunks_path = index_path / "chunks.jsonl"
    if not manifest_path.exists() or not chunks_path.exists():
        return {
            "ready": False,
            "index_dir": str(index_path),
            "file_count": 0,
            "chunk_count": 0,
            "documents": [],
        }

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    chunk_count = int(manifest.get("chunk_count") or 0)
    return {
        "ready": chunk_count > 0,
        "index_dir": manifest.get("index_dir") or str(index_path),
        "file_count": int(manifest.get("file_count") or 0),
        "chunk_count": chunk_count,
        "source_dir": manifest.get("source_dir"),
        "created_at": manifest.get("created_at"),
        "documents": list_index_documents(index_path),
    }


@app.post("/api/lite/index")
async def index_documents(payload: IndexRequest):
    stats = build_index(payload.source_dir, payload.index_dir)
    return stats.__dict__


@app.post("/api/lite/index/upload")
async def index_uploaded_documents(
    files: List[UploadFile] = File(...),
    index_dir: str = Form(default=str(DEFAULT_INDEX_DIR)),
):
    nodes = []
    for file in files:
        suffix = Path(file.filename or "").suffix.lower()
        if suffix not in SUPPORTED_EXTENSIONS:
            continue
        content = await file.read()
        nodes.extend(
            extract_document_nodes_from_bytes(
                file.filename or "untitled",
                content,
            )
        )

    if not nodes:
        return {
            "source_dir": "browser_upload",
            "index_dir": str(Path(index_dir).resolve()),
            "file_count": 0,
            "chunk_count": 0,
        }

    stats = build_index_from_nodes(
        nodes,
        index_dir,
        source_label="browser_upload",
    )
    return stats.__dict__


@app.get("/api/lite/documents")
async def list_documents(index_dir: str = str(DEFAULT_INDEX_DIR)):
    return {
        "index_dir": str(Path(index_dir).expanduser().resolve()),
        "documents": list_index_documents(index_dir),
    }


@app.delete("/api/lite/documents")
async def delete_document(filename: str, index_dir: str = str(DEFAULT_INDEX_DIR)):
    try:
        stats = delete_index_document(filename, index_dir)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return stats.__dict__


@app.post("/api/lite/query")
async def query_documents(payload: QueryRequest):
    sources = search_index(payload.query, payload.index_dir, payload.top_k)
    answer = await answer_query(
        payload.query,
        sources,
        payload.use_llm,
        api_key=payload.api_key,
        base_url=payload.base_url,
        model=payload.model,
    )
    if answer["mode"] == "llm_error":
        return {
            "answer": answer["answer"],
            "mode": answer["mode"],
            "sources": [],
            "retrieved_sources": [],
            "llm": answer.get("llm"),
            "index_manifest": None,
        }
    display_sources = filter_sources_by_answer(answer["answer"], sources, answer["mode"])
    manifest_path = Path(payload.index_dir) / "manifest.json"
    return {
        "answer": answer["answer"],
        "mode": answer["mode"],
        "sources": display_sources,
        "retrieved_sources": sources,
        "llm": answer.get("llm"),
        "index_manifest": manifest_path.as_posix() if manifest_path.exists() else None,
    }


def filter_sources_by_answer(answer: str, sources: list[dict], mode: str = "") -> list[dict]:
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
