from __future__ import annotations

"""
把RAG管线全部串起来：接受文件 → 分块 → 向量化 → 存FAISS → 存DB
"""

import os
from pathlib import Path
from uuid import uuid4
import aiofiles
from fastapi import UploadFile, HTTPException
from sqlalchemy import select, delete
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.config import settings
from app.documents import DocumentNode, NodeType
from app.models.document import Document
from app.models.chunk import Chunk
from app.rag.chunker import TextChunker
from app.rag.embedder import Embedder
from app.rag.vector_store import VectorStore
from app.core.logger import logger


WEB_PARSER_VERSION = "web_legacy_text_v1"


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
            # 4、提取统一文档节点
            nodes = await DocumentService._extract_nodes(
                file_path,
                ext,
                document_id=f"db_document_{doc.id}",
                source_path=file.filename,
            )

            # 5、分块
            chunks = []
            for node in nodes:
                chunks.extend(
                    TextChunker.chunk_node(
                        node,
                        settings.CHUNK_SIZE,
                        settings.CHUNK_OVERLAP,
                    )
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
        """兼容旧调用；内部解析结果统一使用 DocumentNode。"""
        nodes = await DocumentService._extract_nodes(
            file_path,
            file_type,
            document_id=f"file_{Path(file_path).name}",
            source_path=Path(file_path).name,
        )
        return "\n".join(node.content for node in nodes)

    @staticmethod
    async def _extract_nodes(
        file_path: str,
        file_type: str,
        *,
        document_id: str,
        source_path: str,
    ) -> list[DocumentNode]:
        """按现有提取行为生成统一文档节点。"""
        if file_type == "pdf":
            import fitz     #PyMuPDF
            text = ""
            with fitz.open(file_path) as doc:
                for page in doc:
                    text += page.get_text()
        else:
            async with aiofiles.open(file_path, "r", encoding="utf-8") as f:
                text = await f.read()
        if not text.strip():
            return []
        return [
            DocumentNode(
                document_id=document_id,
                content=text,
                parser_version=WEB_PARSER_VERSION,
                node_type=NodeType.TEXT,
                sequence=0,
                source_anchor={"source_path": source_path},
                metadata={
                    "filename": source_path,
                    "file_type": file_type,
                },
            )
        ]

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
