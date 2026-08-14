"""
追问改写模块

将多轮对话中的追问改写为独立问题，解决指代消解。
无 LLM 时直接返回原问题，不阻塞查询流程。
"""

from __future__ import annotations

from app.core.config import settings
from app.security.remote_access import remote_access_enabled


FOLLOWUP_REWRITE_PROMPT = """你是一个问题改写助手。给定一段对话历史和用户的当前问题，将当前问题改写为一个独立的、不需要依赖对话历史就能理解的完整问题。

规则：
- 只做指代消解（如"它"、"那个"、"上一个"、"第三个"、"这个政策"等），将指代词替换为历史中提到的具体内容。
- 如果当前问题本身已经是独立完整的，直接返回原问题。
- 不要增加用户没有问的内容。
- 不要回答问题，只改写成独立问题。
- 只输出改写后的问题，不要加任何解释。

对话历史：
{history}

当前问题：
{query}

改写后的问题："""


async def rewrite_followup(
    query: str,
    history_messages: list[dict],
    *,
    api_key: str = "",
    base_url: str = "",
    model: str = "",
) -> str:
    """将追问改写为独立问题。

    Args:
        query: 用户当前输入
        history_messages: 历史消息列表，每项含 role 和 content
        api_key/base_url/model: LLM 配置

    Returns:
        改写后的独立问题，或原问题（当无 LLM 或失败时）
    """
    if not history_messages or not _should_rewrite(query):
        return query

    api_key = (api_key or settings.LLM_API_KEY or "").strip()
    if not api_key or not remote_access_enabled():
        return query

    history_text = _format_history(history_messages)
    prompt = FOLLOWUP_REWRITE_PROMPT.format(history=history_text, query=query)

    try:
        from openai import AsyncOpenAI

        client = AsyncOpenAI(api_key=api_key, base_url=(base_url or settings.LLM_BASE_URL))
        try:
            response = await client.chat.completions.create(
                model=(model or settings.LLM_MODEL),
                messages=[{"role": "user", "content": prompt}],
                temperature=0.1,
                timeout=15,
            )
            rewritten = (response.choices[0].message.content or "").strip()
            return rewritten if rewritten else query
        finally:
            try:
                await client.close()
            except Exception:
                pass
    except Exception:
        return query


def _should_rewrite(query: str) -> bool:
    """快速判断是否有必要改写。纯规则，不调 LLM。"""
    indicators = [
        "它", "他", "她", "这个", "那个", "这些", "那些",
        "上面", "前面", "刚才", "之前",
        "第一个", "第二个", "第三个", "前面那个",
        "再", "还有", "另外", "别的",
        # 追问引用来源类：需要结合历史定位“哪个文档/哪个结论”
        "哪个文档", "哪个文件", "哪个来源", "哪个资料", "什么文档", "什么文件",
        "出处", "来源", "引用", "哪份",
        # 追问上一步结果类：如"为什么失败/报错/不行"，需结合历史
        "为什么失败", "为什么报错", "为什么错误", "为什么不行", "为什么错",
        "怎么回事", "为什么出错", "失败的原因", "报错原因",
        # 追问上一步为什么没给回答
        "为什么不回答", "为什么没回答", "为什么没有回答", "为什么不回",
        "为什么不回复", "怎么不回答",
    ]
    return any(w in query for w in indicators)


def _format_history(messages: list[dict]) -> str:
    lines = []
    for i, msg in enumerate(messages[-6:], 1):  # 最多最近6条
        role = "用户" if msg.get("role") == "user" else "助手"
        content = msg.get("content", "")
        if content:
            lines.append(f"[{role}] {str(content)[:300]}")
    return "\n".join(lines)
