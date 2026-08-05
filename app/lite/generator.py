from __future__ import annotations

from typing import Any, Optional

from app.core.config import settings
from app.security.redaction import redact_secrets
from app.security.remote_access import remote_access_enabled


LITE_ANSWER_PROMPT = """你是本地知识库问答助手。请只基于给定资料回答用户问题。

要求：
- 直接回答问题，不要扩展到资料外。
- 每个关键结论后标注引用编号，例如 [1]。
- 如果资料不足，回答“当前资料不足以回答”。
- 保留关键数值、时间、比例、名称和流程条件。
- 当用户询问“多久、几天、多少天、时间”时，优先提取资料中的时间数值；如果存在分阶段时间，先分别列出，再给出合计。
- 不要复述与问题无关的图表、模型、参考文献或背景段落。

用户问题：
{query}

资料：
{context}
"""


async def answer_query(
    query: str,
    sources: list[dict[str, Any]],
    use_llm: bool = True,
    *,
    api_key: Optional[str] = None,
    base_url: Optional[str] = None,
    model: Optional[str] = None,
    instructions: str = "",
) -> dict[str, Any]:
    context = build_context(sources)
    resolved_api_key = (api_key or settings.LLM_API_KEY or "").strip()
    resolved_base_url = (base_url or settings.LLM_BASE_URL or "").strip()
    resolved_model = (model or settings.LLM_MODEL or "").strip()
    remote_blocked = bool(use_llm and not remote_access_enabled())
    if remote_blocked:
        use_llm = False

    if use_llm and not resolved_api_key:
        return {
            "answer": "未配置 LLM API Key。请在页面填写 API Key，或取消勾选“使用 LLM 汇总答案”。",
            "mode": "llm_error",
            "context": context,
            "llm": {
                "enabled": False,
                "base_url": resolved_base_url,
                "model": resolved_model,
                "usage": None,
                "error": "missing_api_key",
            },
        }

    if not sources:
        return {
            "answer": "当前知识库没有检索到足够相关的内容。",
            "mode": "empty",
            "context": context,
            "llm": {
                "enabled": bool(resolved_api_key and use_llm),
                "usage": None,
                "error": "offline_mode" if remote_blocked else None,
            },
        }

    if use_llm:
        effective_query = str(query or "").strip()
        if instructions.strip():
            effective_query += "\n\n回答格式要求：" + instructions.strip()
        result = await _answer_with_llm(
            effective_query,
            context,
            resolved_api_key,
            resolved_base_url,
            resolved_model,
        )
        if result["answer"]:
            return {**result, "mode": "llm", "context": context}

        return {
            "answer": f"LLM 请求失败：{result['llm'].get('error') or '没有返回答案'}",
            "mode": "llm_error",
            "context": context,
            "llm": result["llm"],
        }
    else:
        llm_metadata = {
            "enabled": bool(resolved_api_key and use_llm),
            "usage": None,
            "error": "offline_mode" if remote_blocked else None,
        }

    return {
        "answer": extractive_answer(sources),
        "mode": "local_fallback",
        "context": context,
        "llm": llm_metadata,
    }


def build_context(sources: list[dict[str, Any]]) -> str:
    parts = []
    for index, source in enumerate(sources, 1):
        parts.append(f"[{index}] {source.get('filename')} chunk {source.get('chunk_index')}\n{source.get('content', '')}")
    return "\n\n---\n\n".join(parts)


def extractive_answer(sources: list[dict[str, Any]]) -> str:
    lines = []
    for index, source in enumerate(sources, 1):
        text = " ".join(str(source.get("content", "")).split())
        if text:
            lines.append(f"{text[:260]} [{index}]")
    return "\n".join(lines) if lines else "当前资料不足以回答。"


async def _answer_with_llm(query: str, context: str, api_key: str, base_url: str, model: str) -> dict[str, Any]:
    prompt = LITE_ANSWER_PROMPT.format(query=query, context=context)
    if not remote_access_enabled():
        return {
            "answer": "",
            "llm": {
                "enabled": False,
                "base_url": base_url,
                "model": model,
                "usage": None,
                "error": "offline_mode",
            },
        }
    try:
        from openai import AsyncOpenAI

        client = AsyncOpenAI(api_key=api_key, base_url=base_url)
        response = await client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.2,
            timeout=30,
        )
        answer = (response.choices[0].message.content or "").strip()
        return {
            "answer": answer,
            "llm": {
                "enabled": True,
                "base_url": base_url,
                "model": getattr(response, "model", model),
                "usage": _usage_to_dict(getattr(response, "usage", None)),
                "prompt_chars": len(prompt),
                "context_chars": len(context),
                "answer_chars": len(answer),
            },
        }
    except Exception as exc:
        return {
            "answer": "",
            "llm": {
                "enabled": True,
                "base_url": base_url,
                "model": model,
                "usage": None,
                "error": redact_secrets(str(exc)),
            },
        }


def _usage_to_dict(usage: Any) -> Optional[dict]:
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
