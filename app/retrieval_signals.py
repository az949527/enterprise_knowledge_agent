from __future__ import annotations

import re


SUMMARY_QUERY_MARKERS = (
    "讲什么",
    "讲了什么",
    "说什么",
    "说了什么",
    "主要内容",
    "总结",
    "概括",
    "摘要",
    "介绍一下",
    "this document",
    "summarize",
    "summary",
    "overview",
)
SUMMARY_SECTION_MARKERS = (
    "摘要",
    "概述",
    "概要",
    "总结",
    "主要内容",
    "executive summary",
    "overview",
    "summary",
)
PROCESS_QUERY_MARKERS = (
    "如何",
    "怎么",
    "怎样",
    "流程",
    "步骤",
    "办理",
    "操作",
    "谁审批",
    "谁复核",
    "how to",
    "procedure",
    "steps",
    "workflow",
)
PROCESS_TEXT_MARKERS = (
    "流程",
    "步骤",
    "操作",
    "办理",
    "申请",
    "审批",
    "复核",
    "提交",
    "procedure",
    "steps",
    "workflow",
)
TIME_QUERY_MARKERS = (
    "多久",
    "几天",
    "多少天",
    "多长时间",
    "什么时候",
    "何时",
    "时限",
    "期限",
    "有效期",
    "提前",
    "how long",
    "when",
    "deadline",
    "duration",
)
QUANTITY_QUERY_MARKERS = (
    "多少",
    "几个",
    "几家",
    "几次",
    "金额",
    "比例",
    "百分比",
    "how much",
    "how many",
    "percentage",
    "amount",
)
TIME_VALUE_PATTERN = re.compile(
    r"(?:\d+(?:\.\d+)?|[零一二三四五六七八九十百千万两]+)\s*"
    r"(?:分钟|小时|个?工作日|个?自然日|天|周|星期|个月|月|季度|年|"
    r"minutes?|hours?|business days?|calendar days?|days?|weeks?|months?|quarters?|years?)",
    re.IGNORECASE,
)
NUMBER_VALUE_PATTERN = re.compile(
    r"(?:"
    r"\d+(?:[.,]\d+)?\s*(?:%|％|元|万元|亿元|个|家|次|条|人|份|台|套|"
    r"percent|percentage|usd|cny|items?|people|times?)?"
    r"|[零一二三四五六七八九十百千万两]+\s*(?:%|％|元|万元|亿元|个|家|次|条|人|份|台|套)"
    r")",
    re.IGNORECASE,
)
DEADLINE_TEXT_MARKERS = (
    "内",
    "前",
    "后",
    "到期",
    "截止",
    "有效期",
    "不超过",
    "至少提前",
    "within",
    "before",
    "after",
    "deadline",
    "expires",
)
URL_PATTERN = re.compile(r"https?://|www\.", re.IGNORECASE)
EMAIL_PATTERN = re.compile(r"\b[\w.+-]+@[\w.-]+\.[a-z]{2,}\b", re.IGNORECASE)
PAGE_NUMBER_PATTERN = re.compile(
    r"^(?:第\s*)?\d+\s*(?:页|/\s*\d+)?$|^page\s+\d+(?:\s+of\s+\d+)?$",
    re.IGNORECASE,
)


def looks_like_summary_query(query: str) -> bool:
    text = str(query or "").casefold()
    return any(marker in text for marker in SUMMARY_QUERY_MARKERS)


def query_intent_bonus(query: str, text: str) -> float:
    query_text = str(query or "").casefold()
    text_text = str(text or "").casefold()
    if not query_text or not text_text:
        return 0.0

    bonus = 0.0
    if any(marker in query_text for marker in TIME_QUERY_MARKERS):
        time_values = TIME_VALUE_PATTERN.findall(text_text)
        if time_values:
            bonus += min(0.8, 0.35 + 0.15 * (len(time_values) - 1))
        if any(marker in text_text for marker in DEADLINE_TEXT_MARKERS):
            bonus += 0.2

    if any(marker in query_text for marker in QUANTITY_QUERY_MARKERS):
        if NUMBER_VALUE_PATTERN.search(text_text):
            bonus += 0.35

    if any(marker in query_text for marker in PROCESS_QUERY_MARKERS):
        matches = sum(marker in text_text for marker in PROCESS_TEXT_MARKERS)
        bonus += min(matches * 0.08, 0.32)

    if looks_like_summary_query(query_text):
        matches = sum(marker in text_text for marker in SUMMARY_SECTION_MARKERS)
        bonus += min(matches * 0.12, 0.36)

    return bonus


def noise_penalty(text: str) -> float:
    text_text = str(text or "").strip()
    if not text_text:
        return 0.8

    penalty = 0.0
    lines = [line.strip() for line in text_text.splitlines() if line.strip()]
    if lines:
        page_number_lines = sum(bool(PAGE_NUMBER_PATTERN.fullmatch(line)) for line in lines)
        penalty += min(page_number_lines / len(lines), 1.0) * 0.5

        unique_ratio = len(set(lines)) / len(lines)
        if len(lines) >= 4 and unique_ratio < 0.6:
            penalty += (0.6 - unique_ratio) * 0.75

    meaningful = sum(character.isalnum() or "\u4e00" <= character <= "\u9fff" for character in text_text)
    if meaningful / max(len(text_text), 1) < 0.35:
        penalty += 0.35

    if len(text_text) < 160 and (URL_PATTERN.search(text_text) or EMAIL_PATTERN.search(text_text)):
        penalty += 0.15

    return min(penalty, 1.0)
