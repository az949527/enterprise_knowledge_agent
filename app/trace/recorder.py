from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List
from uuid import uuid4


def build_query_trace(
    *,
    user_id: int,
    query: str,
    top_k: int,
    chunks: List[dict],
    answer_payload: dict,
    elapsed_ms: int,
    timings: Dict[str, int] | None = None,
) -> Dict[str, Any]:
    trace_id = str(uuid4())
    answer = answer_payload.get("answer", "")
    answer_lines = [line.strip() for line in answer.splitlines() if line.strip()]
    timings = timings or {"total_ms": elapsed_ms}
    return {
        "trace_id": trace_id,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "elapsed_ms": elapsed_ms,
        "timings": timings,
        "steps": [
            {
                "name": "query_received",
                "data": {
                    "user_id": user_id,
                    "query": query,
                    "top_k": top_k,
                },
            },
            {
                "name": "retrieve",
                "elapsed_ms": timings.get("retrieve_ms"),
                "data": {
                    "retrieved_count": len(chunks),
                    "results": [_chunk_trace_item(index, chunk) for index, chunk in enumerate(chunks, start=1)],
                },
            },
            {
                "name": "generate_answer",
                "elapsed_ms": timings.get("generate_ms"),
                "data": {
                    "mode": answer_payload.get("mode"),
                    "strategy": answer_payload.get("strategy"),
                    "answer_candidate_count": len(answer_lines),
                    "llm": answer_payload.get("llm"),
                    "answer": answer,
                },
            },
            {
                "name": "final_response",
                "data": {
                    "has_answer": bool(answer.strip()),
                    "source_count": len(chunks),
                    "context_chars": len(answer_payload.get("context", "")),
                    "elapsed_ms": elapsed_ms,
                    "timings": timings,
                },
            },
        ],
    }


def save_trace(trace: Dict[str, Any], trace_dir: str) -> Path:
    output_dir = Path(trace_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"{trace['trace_id']}.json"
    trace["trace_path"] = path.as_posix()
    safe_trace = _json_safe(trace)
    trace.clear()
    trace.update(safe_trace)
    path.write_text(json.dumps(trace, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def _chunk_trace_item(index: int, chunk: dict) -> dict:
    return {
        "rank": index,
        "chunk_id": chunk.get("chunk_id"),
        "document_id": chunk.get("document_id"),
        "chunk_index": chunk.get("chunk_index"),
        "filename": chunk.get("filename"),
        "score": chunk.get("score"),
        "rerank_score": chunk.get("rerank_score"),
        "content_chars": len(chunk.get("content", "")),
        "expanded_content_chars": len(chunk.get("expanded_content", "")),
        "content_preview": _preview(chunk.get("content", "")),
    }


def _preview(text: str, max_chars: int = 220) -> str:
    normalized = " ".join(str(text).split())
    if len(normalized) <= max_chars:
        return normalized
    return normalized[:max_chars].rstrip() + "..."


def _json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_json_safe(item) for item in value]
    if isinstance(value, tuple):
        return [_json_safe(item) for item in value]
    if hasattr(value, "item"):
        try:
            return value.item()
        except ValueError:
            return str(value)
    return value
