from __future__ import annotations

import re
from time import perf_counter
from typing import Any

from app.core.config import settings
from app.core.logger import logger
from app.security.redaction import redact_secrets
from app.security.remote_access import remote_access_enabled


ANSWER_PROMPT = """你是企业知识库问答助手。请只基于给定资料回答用户问题。

要求：
- 直接回答用户当前问题，不要主动扩展到用户没有询问的范围。
- 如果资料能回答当前问题，不要补充“其他条件/其他方法资料不足”之类的免责声明。
- 只有在资料完全无法回答当前问题时，才回答“当前资料不足以回答”。
- 每个关键结论后面标注引用编号，例如 [1]、[2]。
- 保留资料中的关键数值、时间、比例、英文缩写、模型名和方法名。
- 不要编造资料中没有的信息。
- 回答要简洁、直接。

用户问题：
{query}

资料：
{context}
"""


class RAGAnswerGenerator:
    """Generate an answer from retrieved chunks with a local fallback."""

    async def generate(self, query: str, chunks: list) -> dict:
        context = _build_rag_context(chunks)
        if not chunks:
            return {
                "answer": "当前知识库没有检索到足够相关的内容。",
                "context": context,
                "mode": "empty",
                "strategy": "no_retrieved_chunks",
                "llm": _llm_metadata(context=context),
            }

        if settings.LLM_API_KEY and remote_access_enabled():
            llm_result = await self._generate_with_llm(query, context)
            if llm_result["answer"]:
                return {
                    "answer": llm_result["answer"],
                    "context": context,
                    "mode": "llm",
                    "strategy": "llm_with_retrieved_context",
                    "llm": llm_result["llm"],
                }
            llm_metadata = llm_result["llm"]
        elif settings.LLM_API_KEY:
            llm_metadata = _llm_metadata(
                context=context,
                enabled=False,
                error="offline_mode",
            )
        else:
            llm_metadata = _llm_metadata(context=context)

        answer = self._generate_extractive_answer(query, chunks)
        return {
            "answer": answer,
            "context": context,
            "mode": "local_fallback",
            "strategy": "one_best_sentence_per_top_chunk",
            "llm": {
                **llm_metadata,
                "answer_chars": len(answer),
            },
        }

    async def _generate_with_llm(self, query: str, context: str) -> dict:
        prompt = ANSWER_PROMPT.format(query=query, context=context)
        started = perf_counter()
        if not remote_access_enabled():
            return {
                "answer": "",
                "llm": _llm_metadata(
                    context=context,
                    prompt=prompt,
                    enabled=False,
                    error="offline_mode",
                ),
            }
        try:
            from openai import AsyncOpenAI

            client = AsyncOpenAI(
                api_key=settings.LLM_API_KEY,
                base_url=settings.LLM_BASE_URL,
            )
            response = await client.chat.completions.create(
                model=settings.LLM_MODEL,
                messages=[
                    {
                        "role": "user",
                        "content": prompt,
                    }
                ],
                temperature=0.2,
                timeout=30,
            )
            answer = (response.choices[0].message.content or "").strip()
            return {
                "answer": answer,
                "llm": _llm_metadata(
                    context=context,
                    prompt=prompt,
                    answer=answer,
                    elapsed_ms=int((perf_counter() - started) * 1000),
                    response_model=getattr(response, "model", None),
                    usage=getattr(response, "usage", None),
                ),
            }
        except Exception as exc:
            safe_error = redact_secrets(exc)
            logger.warning(
                "LLM answer generation failed, fallback to extractive answer: %s",
                safe_error,
            )
            return {
                "answer": "",
                "llm": _llm_metadata(
                    context=context,
                    prompt=prompt,
                    elapsed_ms=int((perf_counter() - started) * 1000),
                    error=safe_error,
                ),
            }

    def _generate_extractive_answer(self, query: str, chunks: list) -> str:
        query_terms = {char for char in query if not char.isspace()}
        lines = []
        for index, chunk in enumerate(chunks, start=1):
            sentence = _best_sentence(chunk.get("content", ""), query_terms)
            if sentence:
                lines.append(f"{sentence} [{index}]")
        if not lines:
            return "当前资料不足以回答。"
        return "\n".join(lines)


def _best_sentence(text: str, query_terms: set) -> str:
    sentences = [part.strip() for part in re.split(r"[。！？!?]\s*|\n+", text) if part.strip()]
    if not sentences:
        return text[:240].strip()

    def score(sentence: str) -> int:
        return len(set(sentence) & query_terms)

    best = max(sentences, key=score)
    return _strip_source_citations(best)[:240].strip()


def _build_rag_context(chunks: list[dict]) -> str:
    if not chunks:
        return ""
    parts = [
        "以下是与问题相关的知识库内容（每条可能不完整，请结合你的知识回答）：",
        "---",
    ]
    for index, chunk in enumerate(chunks, 1):
        parts.append(
            f"[{index}] {chunk.get('expanded_content') or chunk.get('content', '')}"
        )
    parts.append("---")
    return "\n\n".join(parts)


def _strip_source_citations(text: str) -> str:
    return re.sub(r"\[\d+\]", "", text)


def _llm_metadata(
    *,
    context: str,
    prompt: str = "",
    answer: str = "",
    elapsed_ms: int | None = None,
    response_model: str | None = None,
    usage: Any = None,
    error: str | None = None,
    enabled: bool | None = None,
) -> dict:
    return {
        "enabled": bool(settings.LLM_API_KEY) if enabled is None else enabled,
        "base_url": settings.LLM_BASE_URL,
        "configured_model": settings.LLM_MODEL,
        "response_model": response_model,
        "elapsed_ms": elapsed_ms,
        "prompt_chars": len(prompt),
        "context_chars": len(context),
        "answer_chars": len(answer),
        "usage": _usage_to_dict(usage),
        "error": error,
    }


def _usage_to_dict(usage: Any) -> dict | None:
    if usage is None:
        return None
    if hasattr(usage, "model_dump"):
        return usage.model_dump()
    if isinstance(usage, dict):
        return usage
    return {
        key: getattr(usage, key, None)
        for key in ("prompt_tokens", "completion_tokens", "total_tokens")
        if getattr(usage, key, None) is not None
    }
