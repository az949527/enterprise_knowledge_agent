from __future__ import annotations

import faiss
import numpy as np
from app.core.config import settings


class VectorStore:
    def __init__(self, index_path: str):
        self.index_path = index_path
        self.index = self._load_or_create() # 从磁盘加载或新建

    def add(self,embeddings: np.ndarray, chunk_ids: list[int]):
        """添加向量索引"""
        self.index.add_with_ids(embeddings, np.array(chunk_ids))

    def search(self, query_embedding: np.ndarray, top_k: int = 3) -> list[tuple[int, float]]:
        distances, indices = self.index.search(query_embedding.reshape(1, -1), top_k)
        # distances[0]和indices[0]长度都是top_k
        # FAISS可能返回-1（结果不足 top_k时的占位符）
        results = []
        for idx, dist in zip(indices[0], distances[0]):
            if idx != -1:
                results.append((int(idx), float(dist)))
        return results

    def delete(self, chunk_ids: list[int]):
        """从索引中删除指定chunk_id"""
        ids_to_remove = np.array(chunk_ids)
        self.index.remove_ids(ids_to_remove)

    def save(self):
        """保存索引到磁盘"""
        faiss.write_index(self.index, self.index_path)

    def _load_or_create(self):
        """加载已有索引，不存在则新建IndexFlatIP"""
        import os
        if os.path.exists(self.index_path):
            return faiss.read_index(self.index_path)
        # IndexFlatIP = 内积（归一化后等于余弦相似度）
        base_index = faiss.IndexFlatIP(settings.EMBEDDING_DIMENSION)
        return faiss.IndexIDMap2(base_index)
