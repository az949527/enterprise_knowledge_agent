from __future__ import annotations

from typing import Any, Dict, Optional, List

from pydantic import BaseModel, ConfigDict, Field
from datetime import datetime


class DocumentUploadResponse(BaseModel):
    """上传文档返回"""
    model_config = ConfigDict(from_attributes=True)

    id: int
    filename: str
    file_size: int
    status: str     #processing/ ready/ failed
    created_at: datetime

class DocumentResponse(BaseModel):
    """文档详情"""
    model_config = ConfigDict(from_attributes=True)

    id: int
    filename: str
    file_path: str
    file_size: int
    file_type: Optional[str] = None
    chunk_count: int
    status: str
    created_at: datetime
    updated_at: datetime

class DocumentListResponse(BaseModel):
    """文档列表（不含content，减少传输量）"""
    model_config = ConfigDict(from_attributes=True)

    id: int
    filename: str
    file_size: int
    file_type: Optional[str] = None
    chunk_count: int
    status: str
    created_at: datetime

class RAGQueryRequest(BaseModel):
    """RAG问答请求"""
    conversation_id: Optional[int] = None
    user_id: int = 1
    query: str = Field(..., min_length=1,description="用户问题")

class RAGQueryResponse(BaseModel):
    """RAG问答响应"""
    answer: str
    context: str = ""
    sources: List[dict] = []    # 引用的知识块 [{content, document_id, chunk_index}]
    trace: Dict[str, Any] = {}
