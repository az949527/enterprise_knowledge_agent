"""
文档模型

记录用户上传的文档元数据
上传后的处理流程：保存文件 → 分块 → 生成向量 → FAISS索引
status字段追踪处理进度：processing → ready / failed
"""

from datetime import datetime,timezone

from sqlalchemy import Integer,String,DateTime,func
from sqlalchemy.orm import Mapped,mapped_column

from app.core.database import Base


class Document(Base):
    """
    文档表
    存储用户上传的文档信息，不存文件内容（内容存在data/documents/下）
    chunk_count在分块完成后更新
    """
    __tablename__ = "documents"
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    filename: Mapped[str] = mapped_column(String(255), nullable=False)  #原始文件名
    file_path: Mapped[str] = mapped_column(String(512), nullable=False) #文件保存路径
    file_size: Mapped[int] = mapped_column(Integer, nullable=False) #文件大小,单位字节
    file_type: Mapped[str] = mapped_column(String(16), nullable=False)  # pdf/txt/md
    chunk_count: Mapped[int] = mapped_column(Integer, default=0)    # 分块数量
    status: Mapped[str] = mapped_column(String(16), default="processing")   # processing / ready / failed
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        server_default=func.now(),
    )

    def __repr__(self) -> str:
        return f"<Document {self.id}: {self.filename}>"
