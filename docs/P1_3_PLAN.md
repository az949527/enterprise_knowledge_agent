# P1-3 聊天与记忆 — 实现方案

## 1. 背景分析

### 当前状态
- **两套 FastAPI 应用**：服务端（`app/main.py`, port 8000）和 Lite（`app/lite/main.py`, port 8010）
- **Lite 用 JSONL 文件存储索引**，不用 SQLAlchemy；服务端用 SQLAlchemy（`app/core/database.py`）
- **Lite query 路径极简**：`search_index`（词法评分）→ `answer_query`（单次 LLM 调用），不走 `query_planner`/`desktop_query`
- **无 SSE/流式**，LLM 调用是同步阻塞的 `await client.chat.completions.create()`
- **无任何会话历史**，每次查询独立

### 关键约束
- Lite app 目前不调用 `init_db()`，SQLAlchemy 仅在服务端 app 启动时初始化
- Lite 是用户主要使用的界面 → P1-3 应优先在 Lite 实现
- Desktop 通过 `query_desktop_index` 走更丰富的编排路径

---

## 2. 存储设计

### 新增 SQLAlchemy 模型（`app/models/`）

**`app/models/conversation.py`** — `Conversation` (表 `conversations`)
| 字段 | 类型 | 说明 |
|------|------|------|
| id | UUID PK (str) | 会话 ID |
| title | String(200) | 会话标题（首条问题截断） |
| created_at | DateTime(tz) | 创建时间 |
| updated_at | DateTime(tz) | 最后活跃时间 |
| message_count | Integer | 消息数缓存（避免 count(*)） |
| is_archived | Boolean | 软删除标记 |

**`app/models/message.py`** — `Message` (表 `messages`)
| 字段 | 类型 | 说明 |
|------|------|------|
| id | Integer PK | 自增 |
| conversation_id | FK → conversations.id | |
| role | String(10) | user / assistant |
| original_query | Text | 用户原始问题 |
| rewritten_query | Text(nullable) | 追问改写后的独立问题 |
| answer | Text | 回答内容 |
| citations | Text(JSON) | 引用来源列表 |
| model | String(100) | 使用的 LLM 模型 |
| token_usage | Text(JSON) | prompt_tokens / completion_tokens / total_tokens |
| error | Text(nullable) | 错误信息 |
| created_at | DateTime(tz) | |

**`app/models/conversation_summary.py`** — `ConversationSummary` (表 `conversation_summaries`)
| 字段 | 类型 | 说明 |
|------|------|------|
| id | Integer PK | 自增 |
| conversation_id | FK → conversations.id | |
| summary | Text | 历史摘要文本 |
| start_message_id | Integer | 摘要覆盖的起始消息 ID |
| end_message_id | Integer | 摘要覆盖的结束消息 ID |
| token_count | Integer | 摘要的 token 估算 |
| created_at | DateTime(tz) | |

**`app/models/retrieval_cache.py`** — `RetrievalCache` (表 `retrieval_cache`)
| 字段 | 类型 | 说明 |
|------|------|------|
| id | Integer PK | 自增 |
| cache_key | String(64) unique indexed | SHA256 of (query, index_dir, top_k) |
| result_json | Text | 检索结果 JSON |
| created_at | DateTime(tz) | |
| last_accessed_at | DateTime(tz) | |
| access_count | Integer | 命中次数 |
| index_version | String(64) | 知识库版本 hash，用于失效检测 |

### 存储策略
- **在 Lite app 和 Desktop 中启用 SQLAlchemy**：Lite 的 lifespan 中调用 `init_db()`（复用现有 `Base.metadata.create_all`）
- SQLite 足够：单用户、本地运行、无并发写入压力
- 复用现有 `app/core/database.py` 的 async session / `get_db()` 模式
- 索引：`conversations.updated_at DESC`, `messages.conversation_id + created_at`, `retrieval_cache.cache_key`

---

## 3. 模块分解

### 3.1 追问改写 — `app/lite/followup_rewriter.py`

```
输入：当前问题 + 最近 N 条历史消息（摘要 + 原文）
输出：独立问题（指代消解后的完整问题）
```

- 纯 LLM 调用：给定对话历史，识别指代（"它"、"那个"、"上一个"、"第三个"）并改写
- 无 API key / 离线时直接返回原问题
- Prompt 要求只做指代消解，不改变原问题意图
- 改写结果保存在 Message.rewritten_query

### 3.2 历史感知生成 — 修改 `app/lite/generator.py`

```
输入：query + context + conversation_history（摘要 + 最近消息）
输出：answer + citations + token_usage
```

- 新增 `HISTORY_AWARE_PROMPT`（与 `LITE_ANSWER_PROMPT` 不同）：
  - 包含"对话历史"段（摘要 + 最近几轮问答）
  - 明确指示：历史只用于理解指代和上下文，旧答案不作为知识事实
- 在 `answer_query()` 新增可选参数 `conversation_history: list[dict] | None`
- 上下文 token 上限通过 prompt 截断实现（`MAX_CONTEXT_TOKENS` 配置）

### 3.3 会话服务 — `app/services/conversation_service.py`

```
ConversationService:
  - create_conversation(db) → Conversation
  - get_conversation(db, conv_id) → Conversation | None
  - list_conversations(db, archived=False, page, page_size) → list[Conversation]
  - archive_conversation(db, conv_id)
  - delete_conversation(db, conv_id)  # 级联删除消息
  - clear_all_conversations(db)
  - export_conversation(db, conv_id) → dict  # JSON 导出格式
  
  - add_message(db, conv_id, message_data) → Message
  - get_messages(db, conv_id, limit, before_id) → list[Message]
  - search_messages(db, keyword) → list[Message]
  
  - generate_summary(db, conv_id) → ConversationSummary  # 当消息过长时生成摘要
  - get_active_context(db, conv_id, max_tokens) → (summary, recent_messages)
  
  - get_cached_retrieval(db, cache_key, index_version) → list | None
  - set_cached_retrieval(db, cache_key, result, index_version)
  - invalidate_retrieval_cache(db)  # 知识库更新时调用
  - cleanup_expired(db, retention_days)  # 定期清理
```

### 3.4 会话路由 — 修改 `app/lite/main.py`

新增端点（所有路径在 `/api/lite/` 下）：

| Method | Path | Purpose |
|--------|------|---------|
| POST | `/api/lite/conversations` | 创建新会话 |
| GET | `/api/lite/conversations` | 列出会话 |
| GET | `/api/lite/conversations/{id}` | 获取会话详情 + 消息列表 |
| PATCH | `/api/lite/conversations/{id}` | 更新会话（标题/归档） |
| DELETE | `/api/lite/conversations/{id}` | 删除会话 |
| GET | `/api/lite/conversations/{id}/export` | 导出会话 JSON |
| GET | `/api/lite/conversations/{id}/messages` | 获取消息（分页） |
| POST | `/api/lite/conversations/{id}/query` | **核心**：带历史的查询 |
| DELETE | `/api/lite/conversations` | 清空所有会话 |

### 3.5 配置新增 — `app/core/config.py`

```python
# P1-3 聊天与记忆
MAX_CONVERSATION_CONTEXT_TOKENS: int = 8000    # 对话上下文 token 上限
MAX_RECENT_MESSAGES: int = 10                   # 最近消息保留条数
CONVERSATION_RETENTION_DAYS: int = 90           # 历史保留天数（0=永久）
RETRIEVAL_CACHE_ENABLED: bool = True            # 检索结果缓存
RETRIEVAL_CACHE_TTL_DAYS: int = 7               # 缓存有效期
```

---

## 4. 查询流程变更

### 当前流程（Lite `/api/lite/query`）
```
query → search_index → answer_query → filter_sources → response
```

### P1-3 流程（新 `/api/lite/conversations/{id}/query`）
```
1. 接收 query + conversation_id
2. 获取会话上下文：
   - get_active_context(conv_id) → (summary, recent_messages)
3. 追问改写（如果有历史）：
   - followup_rewriter.rewrite(query, recent_messages) → rewritten_query
4. 检索（带缓存）：
   - cache_key = hash(rewritten_query, index_dir, top_k)
   - get_cached_retrieval(cache_key, index_version) → 命中直接返回
   - 未命中 → search_index → set_cached_retrieval
5. 生成答案（带历史上下文）：
   - answer_query(rewritten_query, sources, history_context)
   - 使用 HISTORY_AWARE_PROMPT
6. 保存消息：
   - add_message(conv_id, {original_query, rewritten_query, answer, citations, model, token_usage})
7. 检查是否需要生成摘要：
   - 如果近期消息 token 估算超过阈值 → generate_summary
8. 返回 response（含 source 引用 + token usage + rewritten_query）
```

### Desktop 集成
- 在 `query_desktop_index` 同理注入 `conversation_history` 参数
- `QueryWorker` 增加 conversation_id 传递

---

## 5. 数据库初始化

### Lite app 添加 lifespan
```python
# app/lite/main.py
from contextlib import asynccontextmanager
from app.core.database import init_db

@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    yield

app = FastAPI(title="...", lifespan=lifespan)
```

### init_db 扩展
```python
# app/core/database.py init_db()
async def init_db():
    async with engine.begin() as conn:
        from app.models.document import Document
        from app.models.chunk import Chunk
        from app.models.conversation import Conversation
        from app.models.message import Message
        from app.models.conversation_summary import ConversationSummary
        from app.models.retrieval_cache import RetrievalCache
        await conn.run_sync(Base.metadata.create_all)
```

---

## 6. 实施顺序

| 步骤 | 内容 | 依赖 |
|------|------|------|
| 1 | 创建 4 个 SQLAlchemy 模型 | - |
| 2 | Lite app 添加 lifespan + init_db | 1 |
| 3 | `ConversationService` 基础 CRUD | 1 |
| 4 | Lite 会话管理 API 端点 | 2, 3 |
| 5 | 追问改写模块 | - |
| 6 | 历史感知 prompt + generator 改造 | - |
| 7 | `/conversations/{id}/query` 核心端点 | 4, 5, 6 |
| 8 | `ConversationSummary` 自动摘要 | 7 |
| 9 | `RetrievalCache` 持久化缓存 | 1 |
| 10 | 缓存失效 + 定期清理 | 9 |
| 11 | 前端聊天 UI 改造 | 4, 7 |
| 12 | Desktop 会话集成 | 7 |
| 13 | 测试 | 全量 |

---

## 7. 安全与正确性红线

- **追问改写离线回退**：无 LLM 时直接返回原问题，不阻塞查询
- **历史不作为事实**：在 prompt 中明确标注"以下对话历史仅用于理解指代关系，历史中的答案不是知识来源"
- **SQL 注入零暴露面**：全部走 SQLAlchemy ORM，无原始 SQL
- **导出不含敏感信息**：可选择性排除 API key 相关信息
- **删除会话级联**：删除 conversation 时自动级联删除 messages + summaries
