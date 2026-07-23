from __future__ import annotations

"""
把RAG管线全部串起来：接受文件 → 分块 → 向量化 → 存FAISS → 存DB
"""

import os
from uuid import uuid4
import aiofiles
from fastapi import UploadFile, HTTPException
from sqlalchemy import select, delete
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.config import settings
from app.models.document import Document
from app.models.chunk import Chunk
from app.rag.chunker import TextChunker
from app.rag.embedder import Embedder
from app.rag.vector_store import VectorStore
from app.core.logger import logger


class DocumentService:

    @staticmethod
    async def upload(
            db: AsyncSession,
            user_id: int,
            file: UploadFile,
            embedder: Embedder,
            vector_store: VectorStore,
            neo4j=None,  # Neo4j 连接，可选（没 Docker 时为 None）
    ) -> Document:
        # 1、校验文件类型
        allowed_types = {"txt", "md", "pdf"}
        ext = file.filename.split(".")[-1].lower()
        if ext not in allowed_types:
            raise HTTPException(400, f"不支持的文件类型：{ext}")

        # 2、保存文件到data/documents/
        os.makedirs(settings.DOCUMENTS_DIR, exist_ok=True)
        file_path = os.path.join(settings.DOCUMENTS_DIR, f"{uuid4()}_{file.filename}")
        content = await file.read()
        async with aiofiles.open(file_path, "wb") as f:
            await f.write(content)

        # 3、创建Document记录（status=processing）
        doc = Document(
            user_id=user_id,
            filename=file.filename,
            file_path=file_path,
            file_size=len(content),
            file_type=ext,
            status="processing",
        )
        db.add(doc)
        await db.flush()    # 拿到doc.id

        try:
            # 4、提取文本
            text = await DocumentService._extract_text(file_path,ext)

            # 5、分块
            chunks = TextChunker.chunk_text(
                text, settings.CHUNK_SIZE, settings.CHUNK_OVERLAP
            )

            # 6、向量化
            vectors = embedder.embed(chunks)

            # 7、保存Chunk到DB，收集chunk_id
            chunk_records = []
            for i, chunk_text in enumerate(chunks):
                chunk = Chunk(
                    document_id=doc.id,
                    chunk_index=i,
                    content=chunk_text,
                    token_count=len(chunk_text),    # 粗略估算
                )
                db.add(chunk)
                chunk_records.append(chunk)
            await db.flush()    # 拿到chunk.id

            # 8、添加向量到FAISS
            chunk_ids = [c.id for c in chunk_records]
            vector_store.add(vectors, chunk_ids)
            vector_store.save()

            # 9、更新Document状态
            doc.status = "ready"
            doc.chunk_count = len(chunks)
            await db.commit()

            return doc

        except Exception as e:
            await db.rollback()
            doc.status = "failed"
            await db.commit()
            raise HTTPException(500, f"文档处理失败：{str(e)}")

    @staticmethod
    async def _extract_text(file_path: str,file_type: str) -> str:
        """根据文件类型提取文本"""
        if file_type == "pdf":
            import fitz     #PyMuPDF
            text = ""
            with fitz.open(file_path) as doc:
                for page in doc:
                    text += page.get_text()
            return text
        # txt/ md 直接读取
        async with aiofiles.open(file_path, "r", encoding="utf-8") as f:
            return await f.read()

    @staticmethod
    async def list_documents(db: AsyncSession, user_id: int) -> list[Document]:
        result = await db.execute(
            select(Document)
            .where(Document.user_id == user_id)
            .order_by(Document.created_at.desc())
        )
        return result.scalars().all()

    @staticmethod
    async def delete_document(db: AsyncSession, user_id: int, doc_id: int,
                               vector_store: VectorStore):
        doc = await db.get(Document, doc_id)
        if not doc or doc.user_id != user_id:
            raise HTTPException(404, "文档不存在")
        # 删除文件
        if os.path.exists(doc.file_path):
            os.remove(doc.file_path)
        # 删除 chunk → 从 FAISS 删除向量
        result = await db.execute(
            select(Chunk.id).where(Chunk.document_id == doc_id)
        )
        chunk_ids = result.scalars().all()
        if chunk_ids:
            vector_store.delete(chunk_ids)
            vector_store.save()
        await db.execute(delete(Chunk).where(Chunk.document_id == doc_id))
        await db.delete(doc)
        await db.commit()
