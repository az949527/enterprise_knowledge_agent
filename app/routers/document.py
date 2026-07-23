from __future__ import annotations

from typing import List
from time import perf_counter

from fastapi import APIRouter, Depends, UploadFile, File, Request, Query
from sqlalchemy.ext.asyncio import AsyncSession
from app.core.database import get_db
from app.schemas.document import (
    DocumentUploadResponse,
    DocumentListResponse,
    RAGQueryRequest,
    RAGQueryResponse,
)
from app.rag.retriever import RAGRetriever
from app.services.document_service import DocumentService
from app.trace.recorder import build_query_trace, save_trace

router = APIRouter(prefix="/api/v1/documents", tags=["documents"])

@router.post("/upload", response_model=DocumentUploadResponse)
async def upload_document(
    file: UploadFile = File(...),
    user_id: int = Query(1, ge=1),
    request: Request = None,
    db: AsyncSession = Depends(get_db),
):
    """上传文档"""
    return await DocumentService.upload(
        db, user_id, file,
        embedder=request.app.state.embedder,
        vector_store=request.app.state.vector_store,
    )

@router.get("/", response_model=List[DocumentListResponse])
async def list_documents(
    user_id: int = Query(1, ge=1),
    db: AsyncSession = Depends(get_db),
):
    """获取文档列表"""
    return await DocumentService.list_documents(db, user_id)

@router.delete("/{doc_id}", status_code=204)
async def delete_document(
    doc_id: int,
    user_id: int = Query(1, ge=1),
    request: Request = None,
    db: AsyncSession = Depends(get_db),
):
    """删除文档"""
    await DocumentService.delete_document(
        db, user_id, doc_id,
        vector_store=request.app.state.vector_store,
    )


@router.post("/query", response_model=RAGQueryResponse)
async def query_documents(
    payload: RAGQueryRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """检索知识库并返回基于 sources 的答案。"""
    started = perf_counter()
    retriever = RAGRetriever(
        embedder=request.app.state.embedder,
        vector_store=request.app.state.vector_store,
        db=db,
        use_hyde=False,
        use_reranker=bool(request.app.state.reranker),
        reranker=request.app.state.reranker,
    )
    retrieve_started = perf_counter()
    chunks = await retriever.retrieve(
        payload.query,
        top_k=request.app.state.settings.RERANK_CANDIDATE_K,
        top_n=request.app.state.settings.TOP_K_RETRIEVAL,
        user_id=payload.user_id,
    )
    retrieve_elapsed_ms = int((perf_counter() - retrieve_started) * 1000)
    generate_started = perf_counter()
    answer_payload = await request.app.state.answer_generator.generate(payload.query, chunks)
    generate_elapsed_ms = int((perf_counter() - generate_started) * 1000)
    elapsed_ms = int((perf_counter() - started) * 1000)
    trace = build_query_trace(
        user_id=payload.user_id,
        query=payload.query,
        top_k=request.app.state.settings.RERANK_CANDIDATE_K,
        chunks=chunks,
        answer_payload=answer_payload,
        elapsed_ms=elapsed_ms,
        timings={
            "retrieve_ms": retrieve_elapsed_ms,
            "generate_ms": generate_elapsed_ms,
            "total_ms": elapsed_ms,
        },
    )
    save_trace(trace, request.app.state.settings.TRACE_DIR)
    return RAGQueryResponse(
        answer=answer_payload["answer"],
        context=answer_payload["context"],
        sources=chunks,
        trace=trace,
    )
