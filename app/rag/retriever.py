from __future__ import annotations
import re

"""
把前面几步串起来：用户问题 → 向量化 → FAISS搜索 → 从DB取原文 → 组装成prompt上下文
"""
from app.core.config import settings
from app.rag.embedder import Embedder
from app.rag.vector_store import VectorStore
from app.models.chunk import Chunk
from app.models.document import Document
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from app.rag.reranker import Reranker
from app.rag.hyde import HyDE
from app.core.logger import logger
from app.security.redaction import redact_secrets


class RAGRetriever:
    def __init__(
        self,
        embedder,
        vector_store: VectorStore,
        db: AsyncSession,
        use_hyde: bool = False,
        use_reranker: bool = False,
        reranker=None,
    ):
        self.embedder = embedder
        self.vector_store = vector_store
        self.db = db
        self.use_hyde = use_hyde
        self.reranker = reranker or (Reranker() if use_reranker else None)
        self.hyde = HyDE()

    async def retrieve(self, query: str, top_k: int = 10, top_n: int = 3, user_id: int = None) -> list[dict]:
        """检索与向量查询最相关的知识块"""
        results = []
        try:
            # 1、查询转向量（可选 HyDE 改写）
            if self.use_hyde:
                hypo_answer = await self.hyde.generate(query)
                query_vec = self.embedder.embed_query(hypo_answer)
            else:
                query_vec = self.embedder.embed_query(query)

            # 2、FAISS搜索，返回[(chunk_id, similarity), ...]
            results = self.vector_store.search(query_vec, top_k)
        except Exception as exc:
            logger.warning(
                "Vector retrieval failed, fallback to lexical retrieval: %s",
                redact_secrets(exc),
            )
        results = await self._merge_lexical_candidates(query, results, user_id)
        if not results:
            return []

        # 3、从DB查chunk原文（拼查询，避免循环查单条）
        chunk_ids = [cid for cid, _ in results]
        stmt = (
            select(Chunk, Document.filename)
            .join(Document, Chunk.document_id == Document.id)
            .where(Chunk.id.in_(chunk_ids))
        )
        if user_id is not None:
            stmt = stmt.where(Document.user_id == user_id)
        db_result = await self.db.execute(stmt)
        chunk_map = {
            chunk.id: {
                "chunk": chunk,
                "filename": filename,
            }
            for chunk, filename in db_result.all()
        }

        # 4、构建候选列表（只取 DB 存在的数据）
        candidates = [
            (chunk_map[cid]["chunk"].content, score)
            for cid, score in results
            if cid in chunk_map
        ]
        if not candidates:
            return []

        # 5、Reranker 精排（模型不可用时按 FAISS 分数排序）
        if self.reranker:
            reranked = self.reranker.rerank(query, candidates, top_n)
        else:
            reranked = [(c, 0.0) for c in sorted(candidates, key=lambda x: x[1], reverse=True)[:top_n]]

        # 6、相似度阈值过滤 + 构建返回结果
        SIMILARITY_THRESHOLD = 0.3
        valid = []
        for (text, faiss_score), rerank_score in reranked:
            if faiss_score >= SIMILARITY_THRESHOLD:
                meta = _find_chunk_meta(chunk_map, text)
                valid.append({
                    "chunk_id": meta["chunk"].id if meta else None,
                    "document_id": meta["chunk"].document_id if meta else None,
                    "chunk_index": meta["chunk"].chunk_index if meta else None,
                    "filename": meta["filename"] if meta else None,
                    "content": text,
                    "score": faiss_score,
                    "rerank_score": rerank_score,
                })

        await self._attach_expanded_context(valid)
        return valid

    @staticmethod
    def build_rag_context(chunks: list[dict]) -> str:
        """将检索到的块拼接成prompt上下文"""
        if not chunks:
            return ""
        parts = ["以下是与问题相关的知识库内容（每条可能不完整，请结合你的知识回答）：", "---"]
        for i, c in enumerate(chunks, 1):
            parts.append(f"[{i}] {c.get('expanded_content') or c['content']}")
        parts.append("---")
        return "\n\n".join(parts)

    async def _attach_expanded_context(self, chunks: list) -> None:
        """Attach neighboring chunks so local fallback answers are not cut at chunk boundaries."""
        for item in chunks:
            document_id = item.get("document_id")
            chunk_index = item.get("chunk_index")
            if document_id is None or chunk_index is None:
                item["expanded_content"] = item.get("content", "")
                continue

            result = await self.db.execute(
                select(Chunk)
                .where(Chunk.document_id == document_id)
                .where(Chunk.chunk_index >= max(chunk_index - 4, 0))
                .where(Chunk.chunk_index <= chunk_index + 4)
                .order_by(Chunk.chunk_index)
            )
            neighbors = result.scalars().all()
            item["expanded_content"] = "\n".join(chunk.content for chunk in neighbors) or item.get("content", "")

    async def _merge_lexical_candidates(
        self,
        query: str,
        vector_results: list[tuple[int, float]],
        user_id: int = None,
    ) -> list[tuple[int, float]]:
        if settings.HYBRID_LEXICAL_CANDIDATE_K <= 0:
            return vector_results

        query_terms = _lexical_terms(query)
        if not query_terms:
            return vector_results

        stmt = select(Chunk, Document.filename).join(Document, Chunk.document_id == Document.id)
        if user_id is not None:
            stmt = stmt.where(Document.user_id == user_id)
        db_result = await self.db.execute(stmt)

        lexical_results = []
        for chunk, _ in db_result.all():
            lexical_score = _lexical_score(query_terms, chunk.content)
            if lexical_score > 0:
                lexical_results.append((chunk.id, max(0.3, lexical_score)))
        lexical_results.sort(key=lambda item: item[1], reverse=True)

        if not vector_results and not lexical_results and _looks_like_summary_query(query):
            lexical_results = await self._recent_chunk_candidates(user_id)

        merged = {}
        for chunk_id, score in vector_results:
            merged[chunk_id] = max(float(score), merged.get(chunk_id, float("-inf")))
        for chunk_id, score in lexical_results[: settings.HYBRID_LEXICAL_CANDIDATE_K]:
            merged[chunk_id] = max(float(score), merged.get(chunk_id, float("-inf")))

        return sorted(merged.items(), key=lambda item: item[1], reverse=True)

    async def _recent_chunk_candidates(self, user_id: int = None) -> list[tuple[int, float]]:
        stmt = (
            select(Chunk.id)
            .join(Document, Chunk.document_id == Document.id)
            .where(Document.status == "ready")
            .order_by(Document.created_at.desc(), Chunk.chunk_index.asc())
            .limit(settings.HYBRID_LEXICAL_CANDIDATE_K or settings.TOP_K_RETRIEVAL)
        )
        if user_id is not None:
            stmt = stmt.where(Document.user_id == user_id)
        db_result = await self.db.execute(stmt)
        return [(chunk_id, 0.3) for chunk_id in db_result.scalars().all()]


def _find_chunk_meta(chunk_map: dict, text: str):
    for item in chunk_map.values():
        if item["chunk"].content == text:
            return item
    return None


def _lexical_score(query_terms: set, text: str) -> float:
    if not query_terms:
        return 0.0
    text_terms = _lexical_terms(text)
    if not text_terms:
        return 0.0
    return len(query_terms & text_terms) / len(query_terms)


def _lexical_terms(text: str) -> set:
    normalized = str(text).lower()
    latin_terms = set(re.findall(r"[a-z0-9]{2,}", normalized))
    cjk_text = "".join(re.findall(r"[\u4e00-\u9fff]", normalized))
    cjk_terms = {char for char in cjk_text}
    for size in (2, 3, 4):
        cjk_terms.update(cjk_text[index : index + size] for index in range(max(len(cjk_text) - size + 1, 0)))
    return latin_terms | cjk_terms


def _looks_like_summary_query(query: str) -> bool:
    text = str(query).lower()
    markers = (
        "讲什么",
        "说什么",
        "主要内容",
        "总结",
        "概括",
        "摘要",
        "介绍一下",
        "this document",
        "summarize",
        "summary",
        "overview",
    )
    return any(marker in text for marker in markers)
