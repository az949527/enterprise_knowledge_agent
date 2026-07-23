"""
HyDE (Hypothetical Document Embeddings)

用户问题太短时，先用 LLM 生成一段详细的假设回答，
再用假设回答去做向量检索，提升召回率。

流程：
  用户问题 → LLM 生成假设回答 → 假设回答向量化 → FAISS 检索
"""
from openai import AsyncOpenAI

from app.core.config import settings
from app.core.logger import logger

HYDE_PROMPT = """请根据问题生成一段详细的技术回答，用于辅助搜索。

要求：
- 内容充实，包含相关专业术语
- 以陈述句形式输出，不要出现"根据问题"等元描述
- 长度 100-300 字

问题：{query}"""


class HyDE:
    """HyDE 假设回答生成器"""

    def __init__(self):
        self.client = AsyncOpenAI(
            api_key=settings.LLM_API_KEY,
            base_url=settings.LLM_BASE_URL,
        )

    async def generate(self, query: str) -> str:
        """生成假设回答"""
        try:
            response = await self.client.chat.completions.create(
                model=settings.LLM_MODEL,
                messages=[{"role": "user", "content": HYDE_PROMPT.format(query=query)}],
                temperature=0.3,
                timeout=15,
            )
            hypo_answer = response.choices[0].message.content or ""
            logger.info("【HyDE】问题='%s' → 生成假设回答 %s 字", query[:30], len(hypo_answer))
            return hypo_answer
        except Exception as e:
            logger.warning("【HyDE 生成失败】%s，回退原始查询", e)
            return query
