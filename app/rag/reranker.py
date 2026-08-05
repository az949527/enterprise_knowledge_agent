import os
import re

from app.core.config import settings
from app.core.logger import logger
from app.retrieval_signals import noise_penalty, query_intent_bonus
from app.security.redaction import redact_secrets
from app.security.remote_access import remote_access_enabled


class Reranker:
    def __init__(self, model_name: str = None, use_model: bool = None):
        use_model = settings.RERANKER_USE_MODEL if use_model is None else use_model
        model_name = model_name or settings.RERANKER_MODEL
        self.model_name = model_name
        allow_remote = remote_access_enabled()
        self.local_files_only = (
            settings.RERANKER_LOCAL_FILES_ONLY or not allow_remote
        )
        self.model = None

        # 国内用户用 hf-mirror.com 加速下载
        if allow_remote and "HF_ENDPOINT" not in os.environ:
            os.environ["HF_ENDPOINT"] = "https://hf-mirror.com"

        if not use_model:
            logger.info("【Reranker】使用本地规则精排，不加载 CrossEncoder")
            return

        try:
            from sentence_transformers import CrossEncoder

            self.model = CrossEncoder(model_name, local_files_only=self.local_files_only)
            load_mode = "本地缓存" if self.local_files_only else "自动下载/缓存"
            logger.info("【Reranker】模型 %s 加载成功（%s）", model_name, load_mode)
        except Exception as exc:
            logger.warning(
                "【Reranker】模型 %s 加载失败: %s，回退到本地规则精排",
                model_name,
                redact_secrets(exc),
            )

    def rerank(self, query: str, candidates: list, top_n: int = 3):
        """候选格式：[(chunk_text, faiss_score),...]"""
        if self.model is None or not candidates:
            ranked = sorted(
                candidates,
                key=lambda candidate: _local_rerank_score(query, candidate[0], candidate[1]),
                reverse=True,
            )
            return [
                (candidate, float(_local_rerank_score(query, candidate[0], candidate[1])))
                for candidate in ranked[:top_n]
            ]

        pairs = [(query, text) for text, _ in candidates]
        scores = self.model.predict(pairs)  # 算相关性分数
        ranked = sorted(
            ((candidate, float(score)) for candidate, score in zip(candidates, scores)),
            key=lambda x: x[1],
            reverse=True,
        )
        return ranked[:top_n]


def _local_rerank_score(query: str, text: str, faiss_score: float) -> float:
    query_terms = _terms(query)
    text_terms = _terms(text)
    if not query_terms:
        lexical = 0.0
    else:
        lexical = len(query_terms & text_terms) / len(query_terms)

    score = lexical * 2.0 + float(faiss_score) * 0.35
    score += query_intent_bonus(query, text)
    score -= noise_penalty(text)
    return score


def _terms(text: str) -> set:
    normalized = str(text).lower()
    latin_terms = set(re.findall(r"[a-z0-9]{2,}", normalized))
    cjk_terms = {char for char in normalized if "\u4e00" <= char <= "\u9fff"}
    return latin_terms | cjk_terms
