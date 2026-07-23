from __future__ import annotations

import os

import numpy as np


class Embedder:
    """惰性加载的 Embedding 模型封装

    模型在首次调用 embed() / embed_query() 时加载，而不是在 __init__ 时加载。
    这样启动时不会占用 ~500MB 内存加载模型，避免 AutoDL 容器 2GB cgroup 限制下 OOM。
    """

    def __init__(self, model_name: str):
        self.model_name = model_name
        self._model = None

    def _load_model(self):
        if self._model is not None:
            return
        if "HF_ENDPOINT" not in os.environ:
            os.environ["HF_ENDPOINT"] = "https://hf-mirror.com"
        # 延迟导入：sentence_transformers 包本身加载 torch/transformers 需要 ~450MB
        from sentence_transformers import SentenceTransformer
        self._model = SentenceTransformer(self.model_name)

    def embed(self, texts: list[str]) -> np.ndarray:
        self._load_model()
        return self._model.encode(texts, normalize_embeddings=True)

    def embed_query(self, text: str) -> np.ndarray:
        return self.embed([text])[0]