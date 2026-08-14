"""
检索缓存模型

缓存检索结果（SHA256 key），避免重复查询相同问题时重新检索。
知识库更新后通过 index_version 检测失效。
"""

from datetime import datetime, timezone

from sqlalchemy import Integer, String, DateTime, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class RetrievalCache(Base):
    """检索缓存表"""
    __tablename__ = "retrieval_cache"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    cache_key: Mapped[str] = mapped_column(
        String(64), nullable=False, unique=True, index=True
    )
    result_json: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        server_default=func.now(),
    )
    last_accessed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        server_default=func.now(),
    )
    access_count: Mapped[int] = mapped_column(Integer, default=0)
    index_version: Mapped[str | None] = mapped_column(String(64), nullable=True)

    def __repr__(self) -> str:
        return f"<RetrievalCache {self.cache_key}>"
