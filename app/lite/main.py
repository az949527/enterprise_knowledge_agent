from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
import re
import sys
from typing import List, Optional

from fastapi import FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app.core.config import settings
from app.core.database import init_db, async_session_factory
from app.lite.followup_rewriter import rewrite_followup
from app.lite.generator import answer_query
from app.lite.indexer import (
    DEFAULT_INDEX_DIR,
    SUPPORTED_EXTENSIONS,
    build_index,
    build_index_from_nodes,
    delete_index_document,
    extract_document_nodes_from_bytes,
    list_index_documents,
    rebuild_index,
)
from app.lite.index_diagnostics import diagnose_index
from app.lite.search import search_index
from app.security.remote_access import set_remote_access


set_remote_access(settings.REMOTE_ACCESS_ENABLED)


class IndexRequest(BaseModel):
    source_dir: str = Field(default="demo_documents")
    index_dir: str = Field(default=str(DEFAULT_INDEX_DIR))
    force_rebuild: bool = False


class QueryRequest(BaseModel):
    query: str = Field(..., min_length=1)
    index_dir: str = Field(default=str(DEFAULT_INDEX_DIR))
    top_k: int = Field(default=5, ge=1, le=20)
    use_llm: bool = True
    api_key: Optional[str] = None
    base_url: Optional[str] = None
    model: Optional[str] = None


class ConversationCreateRequest(BaseModel):
    title: str = "新对话"


class ConversationUpdateRequest(BaseModel):
    title: Optional[str] = None
    is_archived: Optional[bool] = None


class ConversationQueryRequest(BaseModel):
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


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    yield


app = FastAPI(title="Local Knowledge Tool Lite", lifespan=lifespan)
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
    if payload.force_rebuild:
        stats = rebuild_index(payload.source_dir, payload.index_dir)
    else:
        stats = build_index(payload.source_dir, payload.index_dir)
    return stats.__dict__


@app.get("/api/lite/index/diagnostics")
async def index_diagnostics(index_dir: str = str(DEFAULT_INDEX_DIR)):
    return diagnose_index(index_dir)


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


# ==================== P1-3 会话管理 ====================


@app.post("/api/lite/conversations")
async def create_conversation(payload: ConversationCreateRequest):
    from app.services.conversation_service import ConversationService

    async with async_session_factory() as db:
        try:
            conv = await ConversationService.create_conversation(db, payload.title)
            await db.commit()
            return _conv_to_dict(conv)
        except Exception:
            await db.rollback()
            raise HTTPException(status_code=500, detail="创建会话失败")


@app.get("/api/lite/conversations")
async def list_conversations(
    archived: bool = False,
    page: int = Query(default=0, ge=0),
    page_size: int = Query(default=50, ge=1, le=200),
):
    from app.services.conversation_service import ConversationService

    async with async_session_factory() as db:
        convs = await ConversationService.list_conversations(
            db, archived=archived, page=page, page_size=page_size
        )
        return {"conversations": [_conv_to_dict(c) for c in convs]}


@app.get("/api/lite/conversations/{conv_id}")
async def get_conversation(conv_id: str):
    from app.services.conversation_service import ConversationService

    async with async_session_factory() as db:
        conv = await ConversationService.get_conversation(db, conv_id)
        if conv is None:
            raise HTTPException(status_code=404, detail="会话不存在")
        messages = await ConversationService.get_messages(db, conv_id, limit=50)
        return {
            **_conv_to_dict(conv),
            "messages": [_msg_to_dict(m) for m in messages],
        }


@app.patch("/api/lite/conversations/{conv_id}")
async def update_conversation(conv_id: str, payload: ConversationUpdateRequest):
    from app.services.conversation_service import ConversationService

    async with async_session_factory() as db:
        kwargs = {k: v for k, v in payload.model_dump().items() if v is not None}
        conv = await ConversationService.update_conversation(db, conv_id, **kwargs)
        if conv is None:
            await db.rollback()
            raise HTTPException(status_code=404, detail="会话不存在")
        await db.commit()
        return _conv_to_dict(conv)


@app.delete("/api/lite/conversations/{conv_id}")
async def delete_conversation(conv_id: str):
    from app.services.conversation_service import ConversationService

    async with async_session_factory() as db:
        ok = await ConversationService.delete_conversation(db, conv_id)
        if not ok:
            await db.rollback()
            raise HTTPException(status_code=404, detail="会话不存在")
        await db.commit()
        return {"deleted": True}


@app.delete("/api/lite/conversations")
async def clear_all_conversations():
    from app.services.conversation_service import ConversationService

    async with async_session_factory() as db:
        try:
            count = await ConversationService.clear_all_conversations(db)
            await db.commit()
            return {"deleted_count": count}
        except Exception:
            await db.rollback()
            raise HTTPException(status_code=500, detail="清空会话失败")


@app.get("/api/lite/conversations/{conv_id}/export")
async def export_conversation(conv_id: str):
    from app.services.conversation_service import ConversationService

    async with async_session_factory() as db:
        data = await ConversationService.export_conversation(db, conv_id)
        if data is None:
            raise HTTPException(status_code=404, detail="会话不存在")
        return data


# ==================== P1-3 核心：带历史的查询 ====================


@app.post("/api/lite/conversations/{conv_id}/query")
async def query_with_conversation(conv_id: str, payload: ConversationQueryRequest):
    from app.services.conversation_service import ConversationService

    async with async_session_factory() as db:
        conv = await ConversationService.get_conversation(db, conv_id)
        if conv is None:
            raise HTTPException(status_code=404, detail="会话不存在")

        # 1. 获取历史上下文
        summary_text, recent_msgs = await ConversationService.get_active_context(
            db, conv_id
        )

        # 2. 构建对话历史
        history_for_rewrite = []
        if summary_text:
            history_for_rewrite.append(
                {"role": "assistant", "content": f"[历史摘要] {summary_text}"}
            )
        for m in recent_msgs:
            if m.role == "user" and m.original_query:
                history_for_rewrite.append(
                    {"role": "user", "content": m.original_query}
                )
            elif m.role == "assistant" and m.answer:
                history_for_rewrite.append(
                    {"role": "assistant", "content": m.answer}
                )

        # 3. 追问改写
        rewritten_query = await rewrite_followup(
            payload.query,
            history_for_rewrite,
            api_key=payload.api_key or "",
            base_url=payload.base_url or "",
            model=payload.model or "",
        )

        # 4. 保存用户消息
        await ConversationService.add_message(
            db,
            conv_id,
            role="user",
            original_query=payload.query,
            rewritten_query=(
                rewritten_query if rewritten_query != payload.query else None
            ),
        )

        # 5. 检索
        sources = search_index(
            rewritten_query, payload.index_dir, payload.top_k
        )

        # 6. 生成答案（带历史）
        history_for_answer: list[dict[str, str]] | None = None
        if summary_text or recent_msgs:
            history_for_answer = []
            if summary_text:
                history_for_answer.append(
                    {"role": "assistant", "content": f"[历史摘要] {summary_text}"}
                )
            for m in recent_msgs[-4:]:
                if m.role == "user" and m.original_query:
                    history_for_answer.append(
                        {"role": "user", "content": m.original_query}
                    )
                elif m.role == "assistant" and m.answer:
                    history_for_answer.append(
                        {"role": "assistant", "content": m.answer}
                    )

        answer = await answer_query(
            rewritten_query,
            sources,
            payload.use_llm,
            api_key=payload.api_key,
            base_url=payload.base_url,
            model=payload.model,
            conversation_history=history_for_answer,
        )

        # 7. 保存助手消息
        llm_info = answer.get("llm") or {}
        await ConversationService.add_message(
            db,
            conv_id,
            role="assistant",
            answer=answer["answer"],
            citations=_filtered_sources(answer["answer"], sources, answer["mode"]),
            model=llm_info.get("model") or llm_info.get("configured_model"),
            token_usage=llm_info.get("usage"),
            error=llm_info.get("error") if answer["mode"] == "llm_error" else None,
        )

        # 8. 检查是否需要摘要
        if conv.message_count >= settings.SUMMARY_TRIGGER_MESSAGE_COUNT:
            latest_summary = await ConversationService.get_active_context(
                db, conv_id
            )
            if latest_summary[0] is None:
                await _try_generate_summary(
                    db, conv_id, payload.api_key, payload.base_url, payload.model
                )

        await db.commit()

    # 构建响应
    if answer["mode"] == "llm_error":
        return {
            "answer": answer["answer"],
            "mode": answer["mode"],
            "sources": [],
            "retrieved_sources": [],
            "llm": answer.get("llm"),
            "rewritten_query": rewritten_query,
        }
    display_sources = filter_sources_by_answer(
        answer["answer"], sources, answer["mode"]
    )
    manifest_path = Path(payload.index_dir) / "manifest.json"
    return {
        "answer": answer["answer"],
        "mode": answer["mode"],
        "sources": display_sources,
        "retrieved_sources": sources,
        "llm": answer.get("llm"),
        "rewritten_query": rewritten_query,
        "index_manifest": manifest_path.as_posix() if manifest_path.exists() else None,
    }


def _filtered_sources(
    answer: str, sources: list[dict], mode: str
) -> list[dict]:
    """返回被引用的来源，用于存库。"""
    if mode == "llm_error":
        return []
    return filter_sources_by_answer(answer, sources, mode)


async def _try_generate_summary(
    db, conv_id: str, api_key: str | None, base_url: str | None, model: str | None
) -> None:
    """尝试使用 LLM 生成对话摘要。失败时静默跳过。"""
    from app.services.conversation_service import ConversationService

    messages = await ConversationService.get_messages(db, conv_id, limit=50)
    if len(messages) < 2:
        return

    api_key = (api_key or settings.LLM_API_KEY or "").strip()
    if not api_key:
        return

    # 取前 80% 的消息做摘要
    summary_count = max(2, int(len(messages) * 0.8))
    target_msgs = messages[:summary_count]
    first_id = target_msgs[0].id if target_msgs else 0
    last_id = target_msgs[-1].id if target_msgs else 0

    history_text = ""
    for m in target_msgs:
        role_label = "用户" if m.role == "user" else "助手"
        content = m.original_query or m.answer or ""
        if content:
            history_text += f"[{role_label}]: {content[:300]}\n"

    summary_prompt = (
        "请用 2-3 句话总结以下对话讨论了哪些主题和关键信息。只输出摘要，不要加其他内容。\n\n"
        f"{history_text}\n\n摘要："
    )

    try:
        from openai import AsyncOpenAI

        client = AsyncOpenAI(
            api_key=api_key,
            base_url=(base_url or settings.LLM_BASE_URL),
        )
        response = await client.chat.completions.create(
            model=(model or settings.LLM_MODEL),
            messages=[{"role": "user", "content": summary_prompt}],
            temperature=0.2,
            timeout=15,
        )
        summary_text = (response.choices[0].message.content or "").strip()
        if summary_text:
            token_count = len(summary_text) // 2  # 粗略中文字符估算
            await ConversationService.generate_summary(
                db, conv_id, summary_text, first_id, last_id, token_count
            )
    except Exception:
        pass  # 摘要失败不影响主流程


def _conv_to_dict(conv) -> dict:
    return {
        "id": conv.id,
        "title": conv.title,
        "created_at": conv.created_at.isoformat() if conv.created_at else None,
        "updated_at": conv.updated_at.isoformat() if conv.updated_at else None,
        "message_count": conv.message_count,
        "is_archived": conv.is_archived,
    }


def _msg_to_dict(msg) -> dict:
    import json as _json

    return {
        "id": msg.id,
        "role": msg.role,
        "original_query": msg.original_query,
        "rewritten_query": msg.rewritten_query,
        "answer": msg.answer,
        "citations": _json.loads(msg.citations) if msg.citations else [],
        "model": msg.model,
        "token_usage": _json.loads(msg.token_usage) if msg.token_usage else None,
        "error": msg.error,
        "created_at": msg.created_at.isoformat() if msg.created_at else None,
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
