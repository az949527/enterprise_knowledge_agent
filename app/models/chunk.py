"""
文档分块模型

文档被切分成多个 chunk 后，每个 chunk 的元数据和原文存在这里。
FAISS 存的是向量 → chunk_id 的映射，原文通过 chunk_id 从这里查询。

删除文档时，chunk 记录和 FAISS 向量需要一起清理。
"""
from datetime import datetime, timezone

from sqlalchemy import Integer, String, DateTime, Text, ForeignKey, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base

class Chunk(Base):
    """
    文档分块表
    存储每个分块的原文和元数据
    token_count用于控制拼入prompt时的总token数
    """
    __tablename__ = "chunks"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    document_id: Mapped[int] = mapped_column(Integer, ForeignKey("documents.id"), nullable=False)
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)   # 分块索引
    content: Mapped[str] = mapped_column(Text, nullable=False)  # 分块内容
    token_count: Mapped[int] = mapped_column(Integer, default=0)    # 分块的token数
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), server_default=func.now()
    )

    def __repr__(self) -> str:
        return f"<Chunk {self.id}: {self.chunk_index}>"
