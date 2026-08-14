"""
会话服务

管理会话、消息、摘要和检索缓存的 CRUD 操作。
所有方法均为 async，使用 SQLAlchemy async session。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, select, delete, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.conversation import Conversation
from app.models.message import Message
from app.models.conversation_summary import ConversationSummary
from app.models.retrieval_cache import RetrievalCache


class ConversationService:

    # ==================== 会话 CRUD ====================

    @staticmethod
    async def create_conversation(db: AsyncSession, title: str = "新对话") -> Conversation:
        conv = Conversation(title=title)
        db.add(conv)
        await db.flush()
        return conv

    @staticmethod
    async def get_conversation(db: AsyncSession, conv_id: str) -> Conversation | None:
        return await db.get(Conversation, conv_id)

    @staticmethod
    async def list_conversations(
        db: AsyncSession,
        *,
        archived: bool = False,
        page: int = 0,
        page_size: int = 50,
    ) -> list[Conversation]:
        stmt = (
            select(Conversation)
            .where(Conversation.is_archived == archived)
            .order_by(Conversation.updated_at.desc())
            .offset(page * page_size)
            .limit(page_size)
        )
        result = await db.execute(stmt)
        return list(result.scalars().all())

    @staticmethod
    async def update_conversation(
        db: AsyncSession, conv_id: str, **kwargs: Any
    ) -> Conversation | None:
        conv = await db.get(Conversation, conv_id)
        if conv is None:
            return None
        for key, value in kwargs.items():
            if hasattr(conv, key):
                setattr(conv, key, value)
        await db.flush()
        return conv

    @staticmethod
    async def archive_conversation(db: AsyncSession, conv_id: str) -> bool:
        conv = await db.get(Conversation, conv_id)
        if conv is None:
            return False
        conv.is_archived = True
        await db.flush()
        return True

    @staticmethod
    async def delete_conversation(db: AsyncSession, conv_id: str) -> bool:
        conv = await db.get(Conversation, conv_id)
        if conv is None:
            return False
        await db.execute(
            delete(ConversationSummary).where(
                ConversationSummary.conversation_id == conv_id
            )
        )
        await db.execute(
            delete(Message).where(Message.conversation_id == conv_id)
        )
        await db.delete(conv)
        await db.flush()
        return True

    @staticmethod
    async def clear_all_conversations(db: AsyncSession) -> int:
        result = await db.execute(select(func.count(Conversation.id)))
        count = result.scalar() or 0
        await db.execute(delete(ConversationSummary))
        await db.execute(delete(Message))
        await db.execute(delete(Conversation))
        await db.flush()
        return count

    @staticmethod
    async def search_conversations(
        db: AsyncSession, keyword: str, limit: int = 20
    ) -> list[Conversation]:
        stmt = (
            select(Conversation)
            .where(Conversation.title.contains(keyword))
            .order_by(Conversation.updated_at.desc())
            .limit(limit)
        )
        result = await db.execute(stmt)
        return list(result.scalars().all())

    @staticmethod
    async def export_conversation(db: AsyncSession, conv_id: str) -> dict | None:
        conv = await db.get(Conversation, conv_id)
        if conv is None:
            return None
        messages = await ConversationService._get_messages(db, conv_id, limit=0)
        return {
            "id": conv.id,
            "title": conv.title,
            "created_at": conv.created_at.isoformat() if conv.created_at else None,
            "updated_at": conv.updated_at.isoformat() if conv.updated_at else None,
            "message_count": conv.message_count,
            "messages": [
                {
                    "role": m.role,
                    "original_query": m.original_query,
                    "rewritten_query": m.rewritten_query,
                    "answer": m.answer,
                    "citations": json.loads(m.citations) if m.citations else [],
                    "model": m.model,
                    "token_usage": json.loads(m.token_usage) if m.token_usage else None,
                    "error": m.error,
                    "created_at": m.created_at.isoformat() if m.created_at else None,
                }
                for m in messages
            ],
        }

    # ==================== 消息 CRUD ====================

    @staticmethod
    async def add_message(
        db: AsyncSession,
        conv_id: str,
        *,
        role: str,
        original_query: str | None = None,
        rewritten_query: str | None = None,
        answer: str | None = None,
        citations: list[dict] | None = None,
        model: str | None = None,
        token_usage: dict | None = None,
        error: str | None = None,
    ) -> Message:
        msg = Message(
            conversation_id=conv_id,
            role=role,
            original_query=original_query,
            rewritten_query=rewritten_query,
            answer=answer,
            citations=json.dumps(citations, ensure_ascii=False) if citations else None,
            model=model,
            token_usage=json.dumps(token_usage, ensure_ascii=False) if token_usage else None,
            error=error,
        )
        db.add(msg)
        # 更新会话消息计数和时间
        conv = await db.get(Conversation, conv_id)
        if conv:
            conv.message_count = (conv.message_count or 0) + 1
            conv.updated_at = datetime.now(timezone.utc)
            # 自动标题：用第一条用户消息
            if conv.title in ("新对话", "") and role == "user" and original_query:
                conv.title = original_query[:80]
        await db.flush()
        return msg

    @staticmethod
    async def get_messages(
        db: AsyncSession,
        conv_id: str,
        *,
        limit: int = 10,
        before_id: int | None = None,
    ) -> list[Message]:
        return await ConversationService._get_messages(
            db, conv_id, limit=limit, before_id=before_id
        )

    @staticmethod
    async def _get_messages(
        db: AsyncSession,
        conv_id: str,
        *,
        limit: int = 10,
        before_id: int | None = None,
    ) -> list[Message]:
        stmt = select(Message).where(Message.conversation_id == conv_id)
        if before_id is not None:
            stmt = stmt.where(Message.id < before_id)
        stmt = stmt.order_by(Message.created_at.asc())
        if limit > 0:
            stmt = stmt.limit(limit)
        result = await db.execute(stmt)
        return list(result.scalars().all())

    @staticmethod
    async def get_active_context(
        db: AsyncSession,
        conv_id: str,
        *,
        max_recent: int | None = None,
    ) -> tuple[str | None, list[Message]]:
        """获取当前会话的活跃上下文：摘要 + 最近消息。
        返回 (summary_text, recent_messages)。
        """
        if max_recent is None:
            max_recent = settings.MAX_RECENT_MESSAGES
        summary_stmt = (
            select(ConversationSummary)
            .where(ConversationSummary.conversation_id == conv_id)
            .order_by(ConversationSummary.created_at.desc())
            .limit(1)
        )
        summary_result = await db.execute(summary_stmt)
        latest_summary = summary_result.scalar()
        summary_text = latest_summary.summary if latest_summary else None

        messages = await ConversationService._get_messages(
            db, conv_id, limit=max_recent
        )
        return summary_text, messages

    @staticmethod
    async def search_messages(
        db: AsyncSession,
        conv_id: str,
        keyword: str,
        limit: int = 20,
    ) -> list[Message]:
        stmt = (
            select(Message)
            .where(
                Message.conversation_id == conv_id,
                Message.original_query.contains(keyword)
                | Message.answer.contains(keyword),
            )
            .order_by(Message.created_at.asc())
            .limit(limit)
        )
        result = await db.execute(stmt)
        return list(result.scalars().all())

    # ==================== 摘要 ====================

    @staticmethod
    async def generate_summary(
        db: AsyncSession,
        conv_id: str,
        llm_summary_text: str,
        start_message_id: int,
        end_message_id: int,
        token_count: int = 0,
    ) -> ConversationSummary:
        summary = ConversationSummary(
            conversation_id=conv_id,
            summary=llm_summary_text,
            start_message_id=start_message_id,
            end_message_id=end_message_id,
            token_count=token_count,
        )
        db.add(summary)
        await db.flush()
        return summary

    # ==================== 检索缓存 ====================

    @staticmethod
    async def get_cached_retrieval(
        db: AsyncSession,
        cache_key: str,
        index_version: str | None = None,
    ) -> list[dict] | None:
        if not settings.RETRIEVAL_CACHE_ENABLED:
            return None
        result = await db.execute(
            select(RetrievalCache).where(RetrievalCache.cache_key == cache_key)
        )
        entry = result.scalar()
        if entry is None:
            return None
        # 检查版本是否匹配
        if index_version and entry.index_version != index_version:
            return None
        # 访问计数
        entry.access_count = (entry.access_count or 0) + 1
        entry.last_accessed_at = datetime.now(timezone.utc)
        await db.flush()
        try:
            return json.loads(entry.result_json)
        except (json.JSONDecodeError, TypeError):
            return None

    @staticmethod
    async def set_cached_retrieval(
        db: AsyncSession,
        cache_key: str,
        result: list[dict],
        index_version: str | None = None,
    ) -> None:
        if not settings.RETRIEVAL_CACHE_ENABLED:
            return
        existing = await db.execute(
            select(RetrievalCache).where(RetrievalCache.cache_key == cache_key)
        )
        entry = existing.scalar()
        if entry is None:
            entry = RetrievalCache(cache_key=cache_key)
            db.add(entry)
        entry.result_json = json.dumps(result, ensure_ascii=False)
        entry.index_version = index_version
        entry.access_count = 0
        entry.last_accessed_at = datetime.now(timezone.utc)
        await db.flush()

    @staticmethod
    async def invalidate_retrieval_cache(db: AsyncSession) -> int:
        result = await db.execute(select(func.count(RetrievalCache.id)))
        count = result.scalar() or 0
        await db.execute(delete(RetrievalCache))
        await db.flush()
        return count

    # ==================== 维护 ====================

    @staticmethod
    async def cleanup_expired(db: AsyncSession) -> int:
        """清理过期的会话和检索缓存。返回清理的会话数。"""
        retention_days = settings.CONVERSATION_RETENTION_DAYS
        deleted_count = 0

        if retention_days > 0:
            cutoff = datetime.now(timezone.utc) - timedelta(days=retention_days)
            # 清理过期会话
            expired = await db.execute(
                select(Conversation.id).where(Conversation.updated_at < cutoff)
            )
            expired_ids = [row[0] for row in expired.all()]
            for conv_id in expired_ids:
                await ConversationService.delete_conversation(db, conv_id)
            deleted_count = len(expired_ids)

        # 清理过期缓存
        ttl_days = settings.RETRIEVAL_CACHE_TTL_DAYS
        if ttl_days > 0:
            cutoff = datetime.now(timezone.utc) - timedelta(days=ttl_days)
            await db.execute(
                delete(RetrievalCache).where(RetrievalCache.last_accessed_at < cutoff)
            )

        await db.flush()
        return deleted_count
