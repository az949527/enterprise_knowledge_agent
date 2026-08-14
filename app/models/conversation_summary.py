"""
对话摘要模型

当会话消息积累超过 token 阈值时，自动将早期消息压缩为摘要。
保留摘要覆盖的消息范围，用于后续上下文裁剪。
"""

from datetime import datetime, timezone

from sqlalchemy import Integer, String, DateTime, Text, ForeignKey, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class ConversationSummary(Base):
    """对话摘要表"""
    __tablename__ = "conversation_summaries"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    conversation_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("conversations.id"), nullable=False, index=True
    )
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    start_message_id: Mapped[int] = mapped_column(Integer, nullable=False)
    end_message_id: Mapped[int] = mapped_column(Integer, nullable=False)
    token_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        server_default=func.now(),
    )

    def __repr__(self) -> str:
        return f"<ConversationSummary {self.id}: msgs {self.start_message_id}-{self.end_message_id}>"
