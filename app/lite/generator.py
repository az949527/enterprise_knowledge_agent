from __future__ import annotations

from typing import Any, Optional

from app.core.config import normalize_llm_model, settings
from app.security.redaction import redact_secrets
from app.security.remote_access import remote_access_enabled


LITE_ANSWER_PROMPT = """你是本地知识库问答助手。请只基于给定资料回答用户问题。

要求：
- 直接回答问题，不要扩展到资料外。
- 每个关键结论后标注引用编号，例如 [1]。
- 如果资料不足，回答"当前资料不足以回答"。
- 保留关键数值、时间、比例、名称和流程条件。
- 当用户询问"多久、几天、多少天、时间"时，优先提取资料中的时间数值；如果存在分阶段时间，先分别列出，再给出合计。
- 不要复述与问题无关的图表、模型、参考文献或背景段落。

用户问题：
{query}

资料：
{context}
"""

HISTORY_AWARE_PROMPT = """你是本地知识库问答助手。请只基于给定资料回答用户问题。

对话历史（以下对话历史仅用于理解指代关系，历史中的答案不作为知识事实）：
{history}

要求：
- 直接回答用户当前问题，不要扩展到资料外。
- 如果当前问题中的指代词（如"它"、"这个"）需要参照对话历史来理解，请先结合历史消解指代，再基于资料回答。
- 每个关键结论后标注引用编号，例如 [1]。
- 如果资料不足，回答"当前资料不足以回答"。
- 保留关键数值、时间、比例、名称和流程条件。
- 不要复述与问题无关的图表、模型、参考文献或背景段落。
- 如果用户询问"哪个文档/哪个文件/出处/来源"（追问上一轮结论的依据），请根据资料中每个来源的文件名直接回答来源，并结合对话历史中上一轮回答标注的引用编号 [N]；此类来源问题不判定为资料不足。

用户当前问题：
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
    conversation_history: Optional[list[dict[str, str]]] = None,
) -> dict[str, Any]:
    context = build_context(sources)
    use_history_aware = bool(conversation_history)
    resolved_api_key = (api_key or settings.LLM_API_KEY or "").strip()
    resolved_base_url = (base_url or settings.LLM_BASE_URL or "").strip()
    resolved_model = normalize_llm_model(model)
    remote_blocked = bool(use_llm and not remote_access_enabled())
    if remote_blocked:
        use_llm = False

    if use_llm and not resolved_api_key:
        return {
            "answer": "未配置 LLM API Key。请在页面填写 API Key，或取消勾选「使用 LLM 汇总答案」。",
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
            "answer": (
                "知识库中没有检索到与您的问题直接相关的内容。\n\n"
                "您可以尝试：\n"
                "1. 换一种更明确的说法，或补充具体对象（如文档名、时间、数值条件）\n"
                "2. 如果是追问，带上完整背景（例如“关于刚才的 XX，具体是……”）\n"
                "3. 如果内容不在知识库中，请先在“知识库”页添加相关文档，再重新提问"
            ),
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
            history_aware=use_history_aware,
            conversation_history=conversation_history,
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
    """拼接检索来源为生成上下文，带总字符上限，避免大表查询 token 膨胀。

    按来源顺序累计；单个来源超过剩余预算时截断到剩余空间并停止追加。
    """
    limit = int(settings.MAX_LLM_CONTEXT_CHARS)
    parts = []
    total = 0
    for index, source in enumerate(sources, 1):
        header = f"[{index}] {source.get('filename')} chunk {source.get('chunk_index')}"
        body = str(source.get("content", ""))
        parent = source.get("parent_content")
        if parent:
            header += "（所属小节上下文；命中的片段已用标记标出）"
            body = f"{parent}\n\n>> 命中的具体片段 <<\n{body}"
        block = f"{header}\n{body}"
        if total + len(block) > limit:
            remaining = limit - total
            if remaining <= 0:
                break
            parts.append(block[:remaining])
            break
        parts.append(block)
        total += len(block)
    return "\n\n---\n\n".join(parts)


def extractive_answer(sources: list[dict[str, Any]]) -> str:
    lines = []
    for index, source in enumerate(sources, 1):
        text = " ".join(str(source.get("content", "")).split())
        if text:
            lines.append(f"{text[:260]} [{index}]")
    return (
        "\n".join(lines)
        if lines
        else "当前资料不足以回答。请换一种更明确的说法，或补充相关文档后再问。"
    )


async def _answer_with_llm(
    query: str,
    context: str,
    api_key: str,
    base_url: str,
    model: str,
    *,
    history_aware: bool = False,
    conversation_history: Optional[list[dict[str, str]]] = None,
) -> dict[str, Any]:
    if history_aware and conversation_history:
        history_text = _format_history_for_prompt(conversation_history)
        prompt = HISTORY_AWARE_PROMPT.format(
            query=query, context=context, history=history_text
        )
    else:
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
        try:
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
        finally:
            try:
                await client.close()
            except Exception:
                pass
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


def _format_history_for_prompt(history: list[dict[str, str]]) -> str:
    """将对话历史格式化为 prompt 可用的文本段。"""
    lines = []
    for msg in history[-8:]:
        role_label = "用户" if msg.get("role") == "user" else "助手"
        content = msg.get("content", "")
        if content:
            lines.append(f"[{role_label}]: {str(content)[:500]}")
    return "\n".join(lines) if lines else "（无历史对话）"
