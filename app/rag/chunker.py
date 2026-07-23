from __future__ import annotations

"""
文档分块工具

将长文本按递归策略切分成小段：
1. 先按段落（\n\n）分割
2. 段落太长则按句号/逗号继续切
3. 相邻 chunk 重叠 50 字符，防止边界信息丢失

用 langchain 的 RecursiveCharacterTextSplitter，比自己手写更健壮。
"""
from langchain_text_splitters import RecursiveCharacterTextSplitter


class TextChunker:
    """文本分块器，将文档切分成适合 RAG 检索的片段"""

    @staticmethod
    def chunk_text(text: str, chunk_size: int = 500, chunk_overlap: int = 50) -> list[str]:
        splitter = RecursiveCharacterTextSplitter(
            chunk_size=chunk_size,
            chunk_overlap=chunk_overlap,
            separators=["\n\n", "\n", "。", "！", "？", "，", " ", ""],
            length_function=len,
        )
        return splitter.split_text(text)
