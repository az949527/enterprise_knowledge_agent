"""P1-2 LLM 兜底：只在确定性路径歧义/需要综合时介入。

安全红线（与 DEVELOPMENT_PLAN P1-2 一致）：
- LLM 只做"拆问题、选参数、写综合段落"。
- 永不写 SQL、不访问原始数据、不执行任意代码。
- 计算结果始终由确定性引擎产出；LLM 的输出会被校验（如选列必须命中白名单）。
- 无 API key / 离线时，本模块不生效，仍走纯规则 + 澄清。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Iterable, Optional

from app.lite.generator import build_context
from app.security.remote_access import remote_access_enabled


async def _llm_call(
    prompt: str,
    *,
    api_key: str,
    base_url: str,
    model: str,
    temperature: float = 0.0,
) -> str:
    from openai import AsyncOpenAI

    client = AsyncOpenAI(api_key=api_key, base_url=base_url)
    try:
        response = await client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            temperature=temperature,
            timeout=30,
        )
        return (response.choices[0].message.content or "").strip()
    finally:
        try:
            await client.close()
        except Exception:
            pass


async def resolve_column(
    query: str,
    columns: Iterable[str],
    *,
    api_key: str,
    base_url: str,
    model: str,
) -> Optional[str]:
    """让 LLM 从白名单里选目标列；输出会校验，不在白名单则返回 None。"""
    column_list = [str(column) for column in columns if column]
    if not column_list:
        return None
    prompt = (
        f"用户问题：{query}\n\n"
        f"可用列：{'、'.join(column_list)}\n\n"
        "请判断这个问题要对哪个列做数值计算（求和/平均/最大/最小/筛选比较）。"
        "只输出一个列名，不要解释。如果无法确定，只输出“无”。"
    )
    try:
        answer = await _llm_call(
            prompt, api_key=api_key, base_url=base_url, model=model
        )
    except Exception:
        return None
    answer = str(answer or "").strip()
    if not answer or answer == "无":
        return None
    for column in column_list:
        if column in answer:
            return column
    compact_answer = re.sub(r"\s+", "", answer)
    for column in column_list:
        if re.sub(r"\s+", "", column) == compact_answer:
            return column
    return None


async def synthesize_mixed(
    query: str,
    comp_answer: str,
    comp_sources: list[dict[str, Any]],
    doc_sources: list[dict[str, Any]],
    *,
    api_key: str,
    base_url: str,
    model: str,
) -> str:
    """把表格计算结果 + 文档检索片段喂给 LLM，写连贯的综合回答。"""
    row_lines = []
    for source in comp_sources:
        row_numbers = source.get("row_numbers") or []
        row_no = row_numbers[0] if row_numbers else "?"
        row_text = str(source.get("content") or "").replace("\n", " / ")
        row_lines.append(f"- {source.get('filename')} 第{row_no}行：{row_text}")
    doc_context = build_context(doc_sources)
    prompt = (
        f"用户问题：{query}\n\n"
        f"【表格计算结果】\n{comp_answer}\n\n"
        f"【命中的表格行】\n" + ("\n".join(row_lines) or "（无）") + "\n\n"
        f"【相关资料】\n{doc_context}\n\n"
        "请综合以上信息回答：表格计算结果要准确引用数字；"
        "相关资料部分给出文档依据；两部分用自然语言组织成完整回答。"
    )
    try:
        return await _llm_call(
            prompt, api_key=api_key, base_url=base_url, model=model, temperature=0.2
        )
    except Exception:
        return ""


async def arun_computation_with_fallback(
    query: str,
    index_dir: str | Path,
    source_paths: Iterable[str] = (),
    *,
    llm_api_key: str = "",
    llm_base_url: str = "",
    llm_model: str = "",
) -> dict[str, Any]:
    """确定性计算；若澄清且 LLM 可用，尝试用 LLM 消歧选列后重算。"""
    from app.lite.structured_query import run_structured_computation

    result = run_structured_computation(query, index_dir, source_paths=source_paths)
    if result["mode"] != "structured_clarify":
        return result
    spec = result.get("spec")
    if not (llm_api_key and remote_access_enabled() and spec and spec.columns):
        return result
    column = await resolve_column(
        query,
        spec.columns,
        api_key=llm_api_key,
        base_url=llm_base_url,
        model=llm_model,
    )
    if not column:
        return result
    retry = run_structured_computation(
        query,
        index_dir,
        source_paths=source_paths,
        column_override=column,
    )
    if retry["mode"] == "structured_clarify":
        return result
    return retry
