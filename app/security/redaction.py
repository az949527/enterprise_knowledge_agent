from __future__ import annotations

import re


_REDACTED = "[REDACTED]"

_PATTERNS = (
    # Authorization: Bearer <token>
    re.compile(
        r"Authorization\s*:\s*Bearer\s+[A-Za-z0-9._~+/= -]+",
        re.IGNORECASE,
    ),
    # 裸 Bearer <token>
    re.compile(r"Bearer\s+[A-Za-z0-9._~+/=-]{8,}", re.IGNORECASE),
    # OpenAI 风格 sk- 密钥
    re.compile(r"sk-[A-Za-z0-9_-]{8,}"),
    # api_key / apikey / secret / token 等键值
    re.compile(
        r"(api[_-]?key|apikey|secret|access[_-]?token|token)\s*[=:]\s*[\"']?[A-Za-z0-9._~+/=-]{4,}",
        re.IGNORECASE,
    ),
)


def redact_secrets(text: object) -> str:
    """移除文本中的 API Key 等敏感值，用于日志和错误消息展示。"""
    if text is None:
        return ""
    result = str(text)
    for pattern in _PATTERNS:
        result = pattern.sub(_REDACTED, result)
    return result
