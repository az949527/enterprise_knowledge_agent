# Next Steps

## 当前状态

按用户要求，第一版脚本闭环已删除，项目切换为借鉴 `membrain` 的后端 RAG 基线。

当前已经具备：

- FastAPI 入口：`app/main.py`
- 前端工作台：`app/static/`，查询页包含 Trace 时间线和 Raw JSON
- 配置、数据库、日志：`app/core/`
- 文档模型：`app/models/document.py`
- 分块模型：`app/models/chunk.py`
- RAG 模块：`app/rag/`
- 模型 reranker：`app/rag/reranker.py`，默认 `BAAI/bge-reranker-v2-m3`，失败时回退本地规则
- 轻量 hybrid retrieval：FAISS 候选 + 关键词候选合并后统一 rerank
- 答案生成模块：`app/rag/generator.py`
- Trace 记录模块：`app/trace/recorder.py`，已记录步骤耗时、LLM 模型和 token usage
- 检索评估脚本：`scripts/eval_retrieval.py`
- RAG 答案评估脚本：`scripts/eval_rag.py`
- 小型评估集：`evals/eval_dataset.json`
- Demo 企业制度文档集：`demo_documents/`
- Demo 企业知识库评估集：`evals/demo_enterprise_eval_dataset.json`，30 条
- Demo 文档加载脚本：`scripts/load_demo_documents.py`
- 轻量本地检索模式：`app/lite/`
- 轻量索引/查询/启动脚本：`scripts/lite_index.py`、`scripts/lite_query.py`、`scripts/run_lite.py`
- 文档服务：`app/services/document_service.py`
- 文档路由：`app/routers/document.py`
- 文档 schema：`app/schemas/document.py`

当前保留的 `membrain` 思路：

```text
上传文件
-> 保存到 data/documents/
-> Document 表记录元数据
-> 提取文本
-> 递归分块
-> embedding
-> Chunk 表保存原文
-> FAISS 保存向量与 chunk_id
-> 查询时 FAISS 找 chunk_id
-> DB 取回 chunk 内容
-> 基于 sources 生成答案
-> 保存并返回 trace
```

当前已验证：

- `python -m compileall app` 通过。
- `from app.main import app` 可成功导入。
- `TestClient(app).get("/health")` 返回 200。
- `GET /`、`/static/styles.css`、`/static/app.js` 返回 200。
- 浏览器打开 `http://127.0.0.1:8000/` 页面无控制台错误。
- `POST /api/v1/documents/query` 返回 `answer`、`context`、`sources`。
- 无 LLM key 时，本地兜底 `answer` 从每个 top chunk 各抽一句候选答案，`sources` 仍保留多条。
- `POST /api/v1/documents/query` 返回 `trace`，并保存到 `outputs/traces/*.json`，包含 retrieve/generate/total 耗时、LLM usage 和 prompt/context/answer 字符数。
- 页面 Trace 区域已展示可读时间线：Overview、Query Received、Retrieve、Generate Answer、Final Response。
- `python scripts/eval_retrieval.py --top-k 5` 可输出检索评估报告。
- `python scripts/eval_rag.py` 可输出端到端 RAG 答案评估报告。
- 默认查询链路启用 hybrid retrieval + reranker：FAISS 召回 10 条候选，关键词补充 5 条候选，再精排返回 5 条。
- 已导入 10 篇 demo 企业制度文档到 user_id=1。
- 轻量模式已可构建 `data/lite_index/`，并通过 `http://127.0.0.1:8010/` 查询；该模式不使用业务数据库、不保存 trace、不加载 embedding/reranker。

首次检索评估结果：

| 指标 | 当前值 |
|------|--------|
| 评估问题数 | 5 |
| Recall Doc@5 | 100.00% |
| Recall Chunk@5 | 60.00% |
| Top1 Doc Hit Rate | 100.00% |
| Top1 Chunk Hit Rate | 0.00% |
| MRR Chunk | 0.217 |
| Avg Latency | 4616 ms |

reranker 对比结果：

| 策略 | Recall Doc@5 | Recall Chunk@5 | Top1 Chunk Hit Rate | MRR Chunk |
|------|--------------|----------------|---------------------|-----------|
| FAISS only | 100.00% | 60.00% | 0.00% | 0.217 |
| FAISS + local reranker | 100.00% | 60.00% | 40.00% | 0.467 |

最近报告：

- `outputs/evals/eval_rag_20260721_100316.md`
- `outputs/evals/eval_rag_20260721_100316.json`
- `outputs/evals/eval_retrieval_20260721_095947.md`
- `outputs/evals/eval_retrieval_20260721_095947.json`
- `outputs/evals/eval_rag_20260721_093004.md`
- `outputs/evals/eval_rag_20260721_093004.json`
- `outputs/evals/eval_rag_20260720_164742.md`
- `outputs/evals/eval_rag_20260720_164742.json`
- `outputs/evals/eval_retrieval_20260719_225041.md`
- `outputs/evals/eval_retrieval_20260719_225041.json`
- `outputs/evals/eval_retrieval_20260719_230319.md`
- `outputs/evals/eval_retrieval_20260719_230319.json`

最近端到端 RAG 评估结果（10 条）：

| 指标 | 当前值 |
|------|--------|
| Recall Chunk@5 | 100.00% |
| Top1 Chunk Hit Rate | 70.00% |
| MRR Chunk | 0.825 |
| Answer Complete Rate | 100.00% |
| Avg Answer Term Coverage | 100.00% |
| Citation Valid Rate | 100.00% |
| Avg Latency | 25863 ms |

最新 LLM usage 冒烟测试（1 条）：

| 指标 | 当前值 |
|------|--------|
| LLM Usage Calls | 1 |
| Prompt Tokens | 7235 |
| Completion Tokens | 395 |
| Total Tokens | 7630 |
| Retrieve Latency | 31132 ms |
| Generate Latency | 5195 ms |

Demo 企业知识库低成本基线（30 条，无 LLM，无 reranker）：

| 指标 | 当前值 |
|------|--------|
| Recall Chunk@5 | 90.00% |
| Top1 Chunk Hit Rate | 80.00% |
| MRR Chunk | 0.836 |
| Answer Complete Rate | 83.33% |
| Avg Answer Term Coverage | 85.00% |
| Citation Valid Rate | 100.00% |
| Avg Latency | 1307 ms |

Demo 企业知识库 reranker 小样本检索评估（5 条）：

| 指标 | 当前值 |
|------|--------|
| Recall Chunk@5 | 100.00% |
| Top1 Chunk Hit Rate | 100.00% |
| MRR Chunk | 1.000 |
| Avg Latency | 37734 ms |

轻量本地工具验证：

| 项目 | 当前结果 |
|------|----------|
| 索引目录 | `data/lite_index/` |
| Demo files | 10 |
| Demo chunks | 10 |
| CLI no-LLM query | 已返回 `demo_remote_work_policy.md` 为 rank 1 |
| Lite Web | `http://127.0.0.1:8010/` 已可查询 |

## 当前目录结构

```text
enterprise_knowledge_agent/
  app/
    core/
    models/
    rag/
    routers/
    schemas/
    services/
    static/
    trace/
  scripts/
  data/
    documents/
    enterprise_knowledge_agent.db
    faiss_index.bin
  evals/
  outputs/
    traces/
    evals/
```

## 下一次开始时优先做

1. 用 demo 企业知识库继续压测。
   - 当前 demo 已有 10 篇文档、30 条评估问题。
   - 低成本基线：`python scripts/eval_rag.py --dataset evals/demo_enterprise_eval_dataset.json --no-llm --no-reranker`
   - 小样本 reranker 检索：`python scripts/eval_retrieval.py --dataset evals/demo_enterprise_eval_dataset.json --top-k 5 --candidate-k 10 --use-reranker --limit 5`
   - 下一步重点看 3 个 retrieval_miss 和 2 个 answer_incomplete 样例。

2. 试用轻量本地工具模式。
   - 构建索引：`python scripts/lite_index.py --source-dir demo_documents`
   - CLI 查询：`python scripts/lite_query.py "远程办公需要提前多久申请？" --no-llm`
   - Web 启动：`python scripts/run_lite.py`
   - 打开：`http://127.0.0.1:8010/`
   - 真实目录测试时，只把 `--source-dir` 或页面 Source Directory 指向明确授权目录。

3. 跑一次端到端 RAG 评估。
   - 无 API 成本冒烟测试：`python scripts/eval_rag.py --no-llm --no-reranker --limit 2`
   - 真实链路评估：`python scripts/eval_rag.py`
   - 重点看 Answer Complete Rate、Avg Answer Term Coverage、Citation Valid Rate。

4. 用前端跑通一次完整操作。
   - 启动：`uvicorn app.main:app --reload`
   - 打开：`http://127.0.0.1:8000/`
   - 上传一个 `.txt` / `.md` / `.pdf`
   - 确认文档列表出现 `chunk_count`
   - 输入问题，确认 sources 非空

5. 前端 Trace 时间线回归检查。
   - 提问后展开页面底部 `Trace`。
   - 确认 Overview、Retrieve、Generate Answer、Final Response 正常显示。
   - 确认 Generate Answer 中能看到模型名、token usage、cache hit/miss 和耗时。
   - Raw JSON 仍保留用于排查。

6. 管理 demo 文档。
   - dry-run：`python scripts/load_demo_documents.py`
   - 首次加载：`python scripts/load_demo_documents.py --load`
   - 替换同名 demo 文档：`python scripts/load_demo_documents.py --load --force`
   - 真实业务数据到位后，保留 demo 集合作为回归测试集。

7. 继续优化 chunk 召回和切分。
   - reranker 已把 Top1 Chunk Hit Rate 从 0% 提升到 40%。
   - 但 Recall Chunk@5 仍是 60%，说明部分正确 chunk 没被召回或标注需要修正。
   - 下一步优先对比 chunk_size、chunk_overlap、PDF 清洗和评估集标注。

8. 改进前端体验。
   - 查询 loading 更明确。
   - sources 支持展开/折叠和复制。
   - 文档列表显示处理失败原因。
   - Trace 支持更易读的时间线展示。

常用命令：

```bash
uvicorn app.main:app --reload
python -m compileall app
python scripts/eval_retrieval.py --top-k 5
python scripts/eval_retrieval.py --top-k 5 --candidate-k 10 --compare-reranker
python scripts/eval_rag.py
python scripts/eval_rag.py --no-llm --no-reranker --limit 2
python scripts/load_demo_documents.py --load
python scripts/eval_rag.py --dataset evals/demo_enterprise_eval_dataset.json --no-llm --no-reranker
python scripts/lite_index.py --source-dir demo_documents
python scripts/lite_query.py "远程办公需要提前多久申请？" --no-llm
python scripts/run_lite.py
```

当前阶段验收：

- `/health` 返回 200。
- `/` 能打开前端工作台。
- 能上传文档。
- `documents` 和 `chunks` 表有记录。
- `data/faiss_index.bin` 有索引。
- `/api/v1/documents/query` 能返回 answer、context 和 sources。
- `/api/v1/documents/query` 能返回 trace，且 trace JSON 落盘。
- 页面能展示文档列表、答案、调试上下文、sources 和 trace。
- Answer Candidates 与 top sources 一一对应，便于快速扫读和定位想要的答案。
- 评估脚本能生成 markdown/json 报告。
- reranker 对比报告能显示 Top1 Chunk Hit Rate 和 MRR 的变化。
- RAG 答案评估报告能显示关键点覆盖率和引用有效率。

## 不要过早做的事

- 不要一开始做复杂独立前端工程。
- 不要一开始接 Neo4j。
- 不要把 `membrain` 的认证、聊天、Neo4j、历史数据一次性搬进来。
- 不要一开始租云服务器。
- 不要一开始做本地大模型部署。
- 不要把金融投资收益当作主指标。

## 第一阶段建议文件

当前已存在：

```text
requirements.txt
app/main.py
app/core/config.py
app/core/database.py
app/rag/chunker.py
app/rag/embedder.py
app/rag/vector_store.py
app/rag/retriever.py
app/rag/hyde.py
app/rag/reranker.py
app/rag/generator.py
app/models/document.py
app/models/chunk.py
app/services/document_service.py
app/routers/document.py
app/trace/recorder.py
app/static/index.html
app/static/styles.css
app/static/app.js
app/lite/
scripts/eval_retrieval.py
scripts/eval_rag.py
scripts/load_demo_documents.py
scripts/lite_index.py
scripts/lite_query.py
scripts/run_lite.py
evals/eval_dataset.json
evals/demo_enterprise_eval_dataset.json
demo_documents/
```

## 每次完成后必须留下的东西

1. 运行命令。
2. 输出样例。
3. 可量化指标。
4. 下一步任务。
5. 如果有评估结果，保存到 `outputs/` 或 `evals/`。
