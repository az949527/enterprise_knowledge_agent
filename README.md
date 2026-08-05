# Enterprise Knowledge Agent

主线定位：做一个“可评估、可追踪的企业知识库 Agent”。

## Python 环境

本项目只支持 Python 3.11。Windows 完整后端环境：

```powershell
.\scripts\install.ps1
.\scripts\run_web.ps1
```

桌面环境：

```powershell
.\scripts\install_desktop.ps1
.\scripts\run_desktop.ps1
```

不要直接使用系统 `python`；完整说明见 `docs/DEVELOPMENT_GUIDE.md`。

本项目从 `investment_agent` 的探索中拆出，长期方向不再押注“投资收益型 Agent”，而是建设一个更通用、更容易落地和展示的知识工作台：

1. 企业文档上传、解析、分块、索引。
2. RAG 问答，回答必须带引用来源。
3. RAG 评估，用指标判断效果，而不是凭感觉。
4. Agent Trace，记录每次检索、工具调用、生成和自检过程。
5. 垂直业务样板，优先复用 `investment_agent` 作为金融投研案例。

## 同级项目分工

| 项目 | 后续定位 |
|------|----------|
| `enterprise_knowledge_agent` | 新主线：企业知识库 + RAG 评估 + Agent Trace |
| `membrain` | 主要参考项目：FastAPI、LangGraph、RAG、文档管理、Trace |
| `investment_agent` | 垂直样板：金融投研报告、回测、资产配置案例 |
| `deepseek_agent` | 只作为 FastAPI、LLM 服务、配置和旧学习记录参考 |
| `fufan-chat-api` | 只参考 RAG、部署、知识库工程形态 |
| `mategen_pro-main` | 只参考交互式数据分析助手的产品形态 |

## 当前最小目标

先完成一个本地可运行闭环：

```text
上传/放入文档
  -> 文档分块
  -> 建立向量索引
  -> 用户提问
  -> 检索相关 chunk
  -> 生成带引用答案
  -> 保存 trace
  -> 可用评估集量化效果
```

## 当前后端入口

当前已删除第一版脚本闭环，切换为借鉴 `membrain` 的后端 RAG 基线，并补了一个轻量前端工作台：

```powershell
.\scripts\run_web.ps1
```

浏览器入口：

```text
http://127.0.0.1:8000/
```

当前默认实现：

- 前端工作台：上传文档、查看列表、删除文档、提交查询、查看答案和 sources
- 文档上传：`POST /api/v1/documents/upload`
- 文档列表：`GET /api/v1/documents/`
- 文档删除：`DELETE /api/v1/documents/{doc_id}`
- RAG 问答：`POST /api/v1/documents/query`
- 文档存储：`data/documents/`
- 元数据存储：SQLite `data/enterprise_knowledge_agent.db`
- 向量索引：FAISS `data/faiss_index.bin`
- 分块：`langchain-text-splitters` 的递归文本切分
- embedding：`sentence-transformers`，默认 `shibing624/text2vec-base-chinese`
- reranker：默认启用 `BAAI/bge-reranker-v2-m3`，FAISS 先召回 10 条，再精排展示 5 条
- hybrid retrieval：FAISS 候选之外，按关键词重叠补充 5 条候选，再统一 rerank
- 答案生成：有 LLM API key 时调用兼容 OpenAI 的模型；无 key 时使用本地抽取式兜底，从每个 top chunk 各抽一句候选答案
- 调试上下文：页面保留 `Retrieved Context` 展开区，便于排查检索结果
- Trace：页面展示查询时间线，并保存 `outputs/traces/*.json`；trace 包含检索/生成耗时、LLM 模型和 token usage
- 检索评估：`python scripts/eval_retrieval.py --top-k 5`
- RAG 答案评估：`python scripts/eval_rag.py`
- Demo 企业制度文档：`demo_documents/`

当前迁移范围：

- 已迁移 `membrain` 的 `app/rag/*`
- 已迁移并适配 `Document` / `Chunk` 模型
- 已迁移并适配 `DocumentService`
- 已迁移并适配文档路由
- 已新增静态前端：`app/static/`
- 已新增 RAG 答案生成：`app/rag/generator.py`
- 已新增基础 Trace：`app/trace/recorder.py`
- 已增强 Trace：记录 retrieve/generate/total 耗时、LLM 模型、prompt/context/answer 字符数和 token usage
- 已新增前端 Trace 时间线，保留 Raw JSON 方便排查
- 已新增检索评估脚本：`scripts/eval_retrieval.py`
- 已新增端到端 RAG 答案评估脚本：`scripts/eval_rag.py`
- 已新增模拟企业制度文档集：`demo_documents/`
- 已新增 demo 文档加载脚本：`scripts/load_demo_documents.py`
- 已新增 30 条 demo 企业知识库评估集：`evals/demo_enterprise_eval_dataset.json`
- 已接入模型 reranker：默认使用 `BAAI/bge-reranker-v2-m3`，模型不可用时回退到本地规则精排
- 已接入轻量 hybrid retrieval 和 expanded context，用于改善召回和生成证据覆盖
- 暂未迁移认证、聊天系统、Neo4j、完整 Agent Trace 页面

## 当前评估入口

```powershell
.\.venv\Scripts\python.exe scripts/eval_retrieval.py --top-k 5
.\.venv\Scripts\python.exe scripts/eval_retrieval.py --top-k 5 --candidate-k 10 --compare-reranker
.\.venv\Scripts\python.exe scripts/eval_rag.py
.\.venv\Scripts\python.exe scripts/eval_rag.py --no-llm --no-reranker --limit 2
.\.venv\Scripts\python.exe scripts/load_demo_documents.py --load
.\.venv\Scripts\python.exe scripts/eval_rag.py --dataset evals/demo_enterprise_eval_dataset.json --no-llm --no-reranker
```

当前小型评估集：

- `evals/eval_dataset.json`
- `evals/demo_enterprise_eval_dataset.json`

评估报告输出：

- `outputs/evals/eval_retrieval_*.md`
- `outputs/evals/eval_retrieval_*.json`
- `outputs/evals/eval_rag_*.md`
- `outputs/evals/eval_rag_*.json`

## 轻量本地工具模式

轻量模式用于发给他人试用本地知识库查询能力：

- 不使用业务数据库
- 不保存 trace
- 不保存查询历史
- 不加载 embedding 模型
- 不加载 reranker
- 只在本地生成索引缓存：`data/lite_index/`
- 只读取用户指定的目录
- 可选使用用户自己的 LLM API key 汇总答案
- 页面中文展示
- LLM 答案引用了哪些编号，页面只展示对应 sources；无引用时展示检索 sources
- 页面支持“选择文件”和“选择文件夹”后自动构建索引，不要求普通用户手动输入路径或点击构建按钮

构建索引：

```powershell
.\.venv-desktop\Scripts\python.exe scripts/lite_index.py --source-dir demo_documents
```

轻量 Web 中也可以直接点击“选择文件”或“选择文件夹”，选择完成后会自动构建索引。

CLI 查询：

```powershell
.\.venv-desktop\Scripts\python.exe scripts/lite_query.py "远程办公需要提前多久申请？" --no-llm
```

启动轻量 Web：

```powershell
.\.venv\Scripts\python.exe scripts/run_lite.py
```

打开：

```text
http://127.0.0.1:8010/
```

## 建议入口

新会话开始时，先读：

1. `docs/DEVELOPMENT_PLAN.md`

确定任务后，再按需查看 `docs/README.md` 中的专题文档和相关代码。
