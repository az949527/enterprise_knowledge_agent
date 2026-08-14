"""
对话会话模型

每个会话包含多轮问答消息。
会话可归档（软删除），支持标题搜索。
"""

from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import Integer, String, DateTime, Boolean, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


def _new_uuid() -> str:
    return uuid4().hex


class Conversation(Base):
    """会话表"""
    __tablename__ = "conversations"

    id: Mapped[str] = mapped_column(
        String(32), primary_key=True, default=_new_uuid
    )
    title: Mapped[str] = mapped_column(
        String(200), nullable=False, default="新对话"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        server_default=func.now(),
    )
    message_count: Mapped[int] = mapped_column(Integer, default=0)
    is_archived: Mapped[bool] = mapped_column(Boolean, default=False)

    def __repr__(self) -> str:
        return f"<Conversation {self.id}: {self.title}>"
