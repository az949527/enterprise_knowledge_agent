"""
=== 通用模块（可复用到其他项目）===

说明：
- 这个文件的结构（继承 BaseSettings + Config 读 .env）是通用的，每个项目都能用
- 字段本身是项目特定的，换项目时按需增删

使用时：
1. 复制整个文件到新项目
2. 修改或删减字段即可
3. 敏感信息放 .env，不要在代码里写死

@see: https://docs.pydantic.dev/latest/api/pydantic_settings/
"""
from pydantic_settings import BaseSettings
from typing import Optional


class Settings(BaseSettings):
    # ==================== 应用基础配置 ====================
    # 【跨项目通用】只要有 Web 服务都需要
    APP_NAME: str = "Enterprise Knowledge Agent"
    DEBUG: bool = False
    REMOTE_ACCESS_ENABLED: bool = False

    # ==================== 数据库配置 ====================
    # 【跨项目通用】只需改连接串即可切换数据库
    DATABASE_URL: str = "sqlite+aiosqlite:///./data/enterprise_knowledge_agent.db"

    # ==================== JWT 认证配置 ====================
    # 【跨项目通用】密钥必须改，其他保持默认即可
    SECRET_KEY: str = "change-this-to-a-random-secret-key"
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 60 * 24 * 7  # 7 days

    # ==================== LLM 配置（项目特定）====================
    # 【业务相关】这个项目用 DeepSeek，换别的 LLM 就改这里
    LLM_API_KEY: str = ""
    LLM_BASE_URL: str = "https://api.deepseek.com"
    LLM_MODEL: str = "deepseek-v4-flash"

    # RAG / Embedding
    EMBEDDING_MODEL: str = "shibing624/text2vec-base-chinese"  # 中文 embedding 模型
    EMBEDDING_DIMENSION: int = 768
    FAISS_INDEX_PATH: str = "./data/faiss_index.bin"  # FAISS 索引文件路径
    DOCUMENTS_DIR: str = "./data/documents"  # 上传文档存储目录
    TRACE_DIR: str = "./outputs/traces"  # 查询 Trace 输出目录
    CHUNK_SIZE: int = 500  # 分块大小（字符数）
    CHUNK_OVERLAP: int = 50  # 分块重叠（字符数）
    TOP_K_RETRIEVAL: int = 5  # 检索返回的最相似块数
    RERANK_CANDIDATE_K: int = 10  # FAISS 先召回的候选数量
    HYBRID_LEXICAL_CANDIDATE_K: int = 5  # 关键词召回补充候选数量
    USE_RERANKER: bool = True  # 是否对 FAISS 候选结果做精排
    RERANKER_USE_MODEL: bool = False  # 是否尝试加载 CrossEncoder 精排模型
    RERANKER_MODEL: str = "BAAI/bge-reranker-v2-m3"
    RERANKER_LOCAL_FILES_ONLY: bool = False  # 是否只从本地缓存加载 reranker 模型

    # P1-1 Parent-Child 自适应检索：小块检索、父章节/父表格用于生成
    PARENT_CONTEXT_ENABLED: bool = True  # 内容类查询默认扩展父上下文
    PARENT_CONTEXT_MAX_PARENT_CHARS: int = 8000  # 单个父节点上下文上限
    PARENT_CONTEXT_MAX_TOTAL_CHARS: int = 40000  # 全部父上下文累计上限

    # P1-2 Excel/CSV 结构化计算
    STRUCTURED_COMPUTATION_ENABLED: bool = True  # 计算类查询走确定性计算
    STRUCTURED_COMPUTATION_MAX_RESULT_ROWS: int = 20  # 结果最多返回行数
    STRUCTURED_COMPUTATION_MAX_SHEET_ROWS: int = 100_000  # 单 Sheet 处理行数上限
    REDIS_URL: str = "redis://localhost:6379/0"

    # ==================== Neo4j 知识图谱配置 ====================
    # 【项目特定】Neo4j 图数据库连接参数
    NEO4J_URL: str = "bolt://localhost:7687"
    NEO4J_USERNAME: str = "neo4j"
    NEO4J_PASSWORD: str = "password123"
    NEO4J_DATABASE: str = "neo4j"

    # ==================== 搜索工具配置 ====================
    SERPAPI_API_KEY: str = ""

    # ==================== 日志配置 ====================
    # 【跨项目通用】
    LOG_LEVEL: str = "INFO"

    # ==================== LangSmith 追踪（可选）====================
    LANGSMITH_TRACING: bool = False
    LANGSMITH_API_KEY: str = ""
    LANGSMITH_PROJECT: str = "membrain"

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


settings = Settings()
