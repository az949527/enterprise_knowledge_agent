# Decisions

## 2026-08-04：P0-8 离线总控与系统凭据存储

决策：

> 完全离线模式默认开启；API Key 从 QSettings 明文迁移到系统凭据库；远程调用前显示数据范围。

实现：

- 桌面设置页新增"完全离线模式"总控，默认开启。开启时模块级门禁
  `app/lite/remote_retrieval.set_remote_access(False)` 禁止一切远程
  LLM、Embedding 和 Reranker 请求，设置页同时禁用远程功能复选。
- 离线时查询自动回退到本地 BM25 + 抽取式答案，用户仍可获得本地回答，
  而不是被拦截报错；需要 LLM 汇总时才关闭离线并授权远程调用。
- 新增 `app/security/credentials.py`：Windows 用 ctypes 调 Credential
  Manager，macOS 用 `security` CLI，其他平台用本地混淆文件兜底，不引入新依赖。
- `SETTINGS_SCHEMA_VERSION` 升到 3，启动时把旧 QSettings 明文 `llm/api_key`
  和 `retrieval/api_key` 迁入凭据库并删除明文键。
- 新增 `app/security/redaction.py` 的 `redact_secrets()`，远程检索与
  LLM 的错误消息统一脱敏，不记录 API Key。
- 首次远程调用前弹出确认框，显示问题内容、将发送的文档片段范围和目标服务
  地址；可选择"同意并记住"（按 Base URL 记忆）或"仅本次同意"。
- 对话页新增联网状态指示；应用临时目录在启动和退出时清理。
- 本阶段不新增遥测；远程调用只发送明确展示的问题与检索片段。

理由：

- Windows 桌面 EXE 是当前验收目标，注册表明文存 API Key 风险最高。
- 默认离线更符合"默认不上传企业文档内容"的定位，用户需要显式关闭离线并同意
  数据外发后才能联网。

## 2026-07-30：文档采用单一进度源

决策：

> `docs/DEVELOPMENT_PLAN.md` 是唯一的阶段计划和开发进度来源，不再并行维护 `ROADMAP.md` 和 `NEXT_STEPS.md`。

文档分工：

- `ACCEPTANCE_REPORTS.md` 保存阶段退出验收证据。
- `DEVELOPMENT_GUIDE.md` 保存环境、命令和开发约定。
- `BUILD_AND_RELEASE.md` 保存跨平台构建发布流程。
- `DECISIONS.md` 继续作为只追加的重要决策日志。
- 专题架构只在对应文档维护，避免复制到进度清单。

理由：

- 原 `ROADMAP.md`、`NEXT_STEPS.md` 和综合优化清单同时描述进度，已经出现阶段状态不一致。
- 发布说明分散在三个文档中，Windows、macOS、桌面版和轻量版难以统一查找。
- 新会话只需先读一个进度文件即可知道当前阶段，减少重复梳理。

## 2026-07-27：项目运行环境统一为 Python 3.11

决策：

> 完整后端、桌面应用、评估脚本和构建流程统一使用 Python 3.11，不再兼容 Python 3.8。

实现：

- `.python-version` 和 `pyproject.toml` 声明 Python 3.11。
- 完整后端环境使用 `.venv`。
- 桌面环境使用 `.venv-desktop`。
- `app/__init__.py` 导入时校验解释器版本。
- Windows 和 macOS 安装、运行、构建脚本拒绝非 Python 3.11。
- 删除 `DocumentNode` 的 Python 3.8 dataclass 兼容分支。

理由：

- 桌面发布和 GitHub Actions 已使用 Python 3.11。
- `dataclass(slots=True)` 等结构层实现不需要继续维护旧版本分支。
- 在 P0-3 解析管线重构前统一版本，可以减少后续双版本测试和兼容成本。

## 2026-07-16：主线方向

决策：

> 主线从 `investment_agent` 的智能投研，调整为企业知识库 + RAG 评估 + Agent Trace。

理由：

- 企业知识库场景更通用，市场需求更广。
- RAG 评估和 Trace 比普通问答更有技术辨识度。
- 数据来源更容易，不依赖昂贵金融数据。
- 可以持续产出可视化、指标、报告和样板案例。
- `investment_agent` 可以保留为金融投研样板，而不是废弃。

## 2026-07-16：资源策略

决策：

> 短期只用本机 + LLM API，不租 GPU，不上 AutoDL。

理由：

- 当前任务是文档处理、RAG、评估、Trace，本机足够。
- API 调用成本低于自建模型推理。
- 云服务器只在需要对外演示或定时任务时再考虑。

## 2026-07-16：项目命名

决策：

> 新目录命名为 `enterprise_knowledge_agent`。

理由：

- 明确表达企业知识库主线。
- 和 `investment_agent` 区分清楚。
- 以后可以承载多个业务样板。

## 2026-07-16：第一阶段 v0 技术栈

决策：

> 第一阶段先用本地 TF-IDF 字符 ngram 向量 + FAISS，答案生成先用抽取式引用回答。

理由：

- 本地环境已安装 `faiss-cpu` 和 `scikit-learn`，可以立即跑通脚本闭环。
- 不依赖首次下载 sentence-transformers 模型，也不依赖外部 LLM API key。
- 对中文企业制度类短文档，字符 ngram 能先支撑可验收的检索效果。
- 后续可以在 `app/rag/embedder.py` 和 `app/rag/generator.py` 中替换为 embedding API 与 LLM 生成。

## 2026-07-17：切换到 membrain 风格后端 RAG 基线

决策：

> 删除第一版脚本闭环实现，迁移并适配 `membrain` 后端中 RAG 相关的核心模块，作为新项目的后端基线。

迁移范围：

- `app/rag/*`
- `Document` / `Chunk` 模型
- `DocumentService`
- 文档路由与 schema
- 最小配置、数据库、日志和 FastAPI 入口

适配原则：

- 不原样搬迁认证、聊天系统、Neo4j、历史数据和缓存。
- 去掉对 `User` 表和 `get_current_user` 的强依赖，先用 `user_id` 查询参数保持接口可测。
- 暂不把实体抽取和 Neo4j 接入主链路，避免图谱依赖阻塞 RAG 后端。
- 保留 `membrain` 的工程化链路：文档入库、chunk 入库、FAISS 存 chunk_id 映射、查询时回表取原文。

## 2026-07-17：新增轻量前端工作台

决策：

> 在当前 FastAPI 后端内直接挂载静态前端页面，先完成可展示、可操作的阶段化产出。

理由：

- 用户后续希望自己和他人都能通过页面使用，而不是只依赖 Swagger。
- 当前阶段只需要上传、列表、删除、查询和 sources 展示，不需要独立前端工程。
- 静态页面部署成本低，和后端一起通过 `uvicorn app.main:app --reload` 运行。

产出：

- `GET /` 打开工作台。
- `app/static/index.html`
- `app/static/styles.css`
- `app/static/app.js`

## 2026-07-17：查询接口升级为答案生成

决策：

> `/api/v1/documents/query` 从只返回 RAG context，升级为返回 `answer + context + sources`。

实现：

- 新增 `app/rag/generator.py`。
- 如果配置了 `LLM_API_KEY`，使用兼容 OpenAI 的接口基于检索上下文生成答案。
- 如果没有 API key 或模型调用失败，回退到本地抽取式答案。
- 前端主区域显示 `Answer`，调试信息放在 `Retrieved Context` 展开区。

理由：

- 页面需要面向真实用户，而不是只展示给开发者看的上下文。
- 保留 context 可以继续支持调试、评估和 Trace。
- 本地兜底保证没有外部 API key 时仍能演示完整链路。

## 2026-07-17：本地兜底答案回退为 top chunk 候选式输出

决策：

> 没有 LLM API key 时，本地抽取式兜底从每个 top chunk 各抽一句最相关的话，并拼成多条候选答案；`sources` 和 `Retrieved Context` 仍保留多条用于核对。

理由：

- 用户实际测试中，正确答案可能排在第 2 个 source。
- “每个 source 一条候选答案”的形式比长段落更容易快速扫读和定位。
- 之前尝试过单条主答案、扩展上下文和噪声过滤，但用户反馈长文本不利于判断。
- 当前回退到更简单、可解释的策略：问题 -> FAISS top chunks -> 每个 chunk 选一句 -> 拼成候选答案。

后续改进：

- 接入 reranker 后，用精排分数改善候选顺序。
- 接入 LLM 后，由模型综合多个 sources 生成自然语言答案。

## 2026-07-19：新增基础 Query Trace

决策：

> 每次 `/api/v1/documents/query` 都生成 trace，接口直接返回，并保存到 `outputs/traces/*.json`。

当前 Trace 步骤：

- `query_received`：记录 user_id、query、top_k。
- `retrieve`：记录检索到的 chunk rank、chunk_id、document_id、chunk_index、filename、score、rerank_score、content_preview。
- `generate_answer`：记录答案生成模式、策略、候选答案数量和 answer。
- `final_response`：记录是否有答案、source 数量、context 字符数、总耗时。

理由：

- 用户已经观察到“分数最高的不一定是最贴切答案”，Trace 可以直接解释排序和候选答案来自哪里。
- 后续做评估时，Trace 可以作为失败样例分析依据。
- 先用 JSON 落盘和前端展示，后续再升级为数据库 trace 表或时间线页面。

## 2026-07-19：新增检索评估雏形

决策：

> 新增 `evals/eval_dataset.json` 和 `scripts/eval_retrieval.py`，先用当前已上传 PDF 构建小型检索评估闭环。

当前评估指标：

- `Recall Doc@K`
- `Recall Chunk@K`
- `Top1 Doc Hit Rate`
- `Top1 Chunk Hit Rate`
- `MRR Chunk`
- `Avg Latency`

首次结果：

- 评估问题数：5
- `Recall Doc@5`: 100.00%
- `Recall Chunk@5`: 60.00%
- `Top1 Chunk Hit Rate`: 0.00%
- `MRR Chunk`: 0.217
- 平均延迟：4616 ms

结论：

- 当前系统能稳定命中文档，但 chunk 排序质量不足。
- 用户观察到的“第 1 条分数高但第 2 条更贴切”已被指标验证。
- 下一步优先做 reranker 或 chunk 策略对比，而不是继续手动调答案展示。

## 2026-07-19：接入 reranker

决策：

> 默认启用模型 reranker：FAISS 先召回 10 条候选，再用 `BAAI/bge-reranker-v2-m3` 精排返回 5 条；模型不可用时回退到本地规则精排。

实现：

- `app/rag/reranker.py` 在不加载 CrossEncoder 的情况下也能工作。
- 默认 `RERANKER_USE_MODEL=True`，`RERANKER_MODEL=BAAI/bge-reranker-v2-m3`。
- 默认允许自动下载/缓存模型；需要离线启动时可设置 `RERANKER_LOCAL_FILES_ONLY=True`。
- 本地规则作为兜底，结合 query 字符/词覆盖、章节信号和噪声惩罚。
- `app/routers/document.py` 查询链路默认使用 `RERANK_CANDIDATE_K=10` 和 `TOP_K_RETRIEVAL=5`。
- `scripts/eval_retrieval.py` 支持 `--use-reranker` 和 `--compare-reranker`。
- 前端 Sources 展示 `rerank_score`。

对比结果：

- `FAISS only`: Top1 Chunk Hit Rate 0.00%，MRR 0.217。
- `FAISS + local reranker`: Top1 Chunk Hit Rate 40.00%，MRR 0.467。

结论：

- reranker 对排序有明显帮助。
- Recall Chunk@5 仍为 60.00%，下一步应优化 chunk 切分、PDF 清洗和评估标注。

## 2026-07-20：进入端到端 RAG 评估

决策：

> 在检索评估之外新增端到端 RAG 答案评估，开始衡量“最终答案是否覆盖关键点、是否带有效引用”。

实现：

- `evals/eval_dataset.json` 从 5 条扩展到 10 条，补充 `reference_answer`。
- 每条样例保留 `expected_document_contains`、`expected_chunk_indices` 和 `expected_answer_terms`。
- 新增 `scripts/eval_rag.py`，复用真实的 `RAGRetriever` 和 `RAGAnswerGenerator`。
- 指标包括 `Answer Complete Rate`、`Avg Answer Term Coverage`、`Answer With Citation Rate`、`Citation Valid Rate`，同时保留检索命中指标。
- 支持 `--no-llm` 做无 API 成本冒烟测试，支持 `--no-reranker` 做基础链路对照。

理由：

- 只看 Recall / MRR 不能判断最终回答是否完整。
- 用户已接入 LLM，下一步需要评估“检索 + rerank + 生成”整体效果。
- 关键点覆盖率是当前阶段成本最低、可解释性最强的答案质量指标。

## 2026-07-20：引入轻量 hybrid retrieval 和 expanded context

决策：

> 在 FAISS 向量召回之外，增加关键词重叠候选；生成上下文优先使用命中 chunk 的相邻扩展内容。

实现：

- 新增配置 `HYBRID_LEXICAL_CANDIDATE_K=5`。
- `app/rag/retriever.py` 在 FAISS 召回后，从当前用户文档的 DB chunks 中按关键词重叠补充候选。
- 中文关键词匹配使用单字和 2-4 字 ngram，降低短语被切散后的漏召回。
- 合并后的候选统一交给 `BAAI/bge-reranker-v2-m3` 精排。
- `build_rag_context` 优先使用 `expanded_content`，让 LLM 能看到相邻 chunk 里的完整证据。
- `TOP_K_RETRIEVAL` 从 3 调整为 5，给生成阶段更多证据。
- prompt 增加“保留关键数值、时间、比例、英文缩写、模型名和方法名”。

最新 10 条评估结果：

- `Recall Chunk@5`: 100.00%
- `Top1 Chunk Hit Rate`: 70.00%
- `MRR Chunk`: 0.825
- `Answer Complete Rate`: 100.00%
- `Avg Answer Term Coverage`: 100.00%
- `Citation Valid Rate`: 100.00%
- `Avg Latency`: 25863 ms

结论：

- 对当前小型评估集，hybrid retrieval 修复了纯向量召回漏掉仪器/采集方式等细节的问题。
- expanded context 修复了正确证据跨 chunk 边界导致 LLM 看不全的问题。
- 目前评估集只有 10 条，下一步必须扩展到至少 30 条，避免过拟合当前 PDF。

## 2026-07-21：增强 Trace 的 LLM usage 和耗时记录

决策：

> 在继续扩业务数据之前，先把每次问答的成本和耗时记录清楚。

实现：

- `app/rag/generator.py` 记录 LLM base_url、配置模型、响应模型、prompt/context/answer 字符数。
- LLM 返回 usage 时，记录 `prompt_tokens`、`completion_tokens`、`total_tokens` 以及 provider 返回的细分字段。
- `app/routers/document.py` 拆分记录 retrieve、generate 和 total 耗时。
- `app/trace/recorder.py` 将 timings 和 LLM usage 写入每次 query trace。
- `scripts/eval_rag.py` 在 case 和 summary 中输出 LLM usage 汇总、平均检索耗时和平均生成耗时。

验证：

- `python scripts/eval_rag.py --limit 1`
- 结果：`LLM Usage Calls=1`，`Total Tokens=7630`，`Retrieve Latency=31132 ms`，`Generate Latency=5195 ms`。

结论：

- 后续可以用本地 trace 直接确认是否调用 LLM、用了哪个模型、消耗多少 token。
- 当前慢点主要在检索/rerank 链路，单条测试中 retrieve 约 31 秒，generate 约 5 秒。

## 2026-07-21：新增模拟企业知识库 demo 集

决策：

> 在真实业务数据到位前，用模拟企业制度文档继续推进多文档 RAG、评估和 Trace 能力。

实现：

- 新增 `demo_documents/`，包含 10 篇企业制度/流程类短文档。
- 新增 `evals/demo_enterprise_eval_dataset.json`，包含 30 条评估问题。
- 新增 `scripts/load_demo_documents.py`，支持 dry-run、`--load` 导入和 `--force` 替换同名 demo 文档。
- `scripts/eval_retrieval.py` 增加 `--limit`，方便 reranker 小样本快速验证。
- 修正评估口径：chunk 命中同时匹配文件名和 chunk_index，避免多文档场景下所有 chunk 0 互相误判。
- 本地 fallback 清理原文参考文献编号，避免把 `[31]` 之类误判为 RAG source 引用。

当前验证：

- `python scripts/load_demo_documents.py` dry-run 通过。
- `python scripts/load_demo_documents.py --load` 已导入 10 篇 demo 文档到 `user_id=1`。
- `python scripts/eval_rag.py --dataset evals/demo_enterprise_eval_dataset.json --no-llm --no-reranker`
- 30 条低成本基线：`Recall Chunk@5=90.00%`，`Top1 Chunk Hit Rate=80.00%`，`Answer Complete Rate=83.33%`，`Citation Valid Rate=100.00%`，`Avg Latency=1307 ms`。
- 5 条 reranker 检索小样本：`Recall Chunk@5=100.00%`，`Top1 Chunk Hit Rate=100.00%`，`MRR Chunk=1.000`，但平均延迟 `37734 ms`，说明 reranker 全量评估成本偏高。

结论：

- demo 企业知识库可以作为真实数据到位前的回归测试集。
- 当前优先优化方向是检索/reranker 延迟，以及 demo 集中剩余的 3 个 retrieval_miss、2 个 answer_incomplete。

## 2026-07-21：前端新增 Trace 时间线

决策：

> 前端只增强查询后的 Trace 可读性，不做评估报告入口和其他管理型页面。

理由：

- 当前产品主要给其他人查询知识库，评估报告属于开发/调试工具，不需要暴露给普通使用者。
- Trace 时间线能解释一次回答如何产生，符合“可追踪、可解释”的主线。
- 保留 Raw JSON，既不丢调试能力，也避免把页面做复杂。

实现：

- `app/static/index.html`：Trace 区域新增 `traceTimeline`，Raw JSON 放到二级折叠。
- `app/static/app.js`：渲染 Overview、Query Received、Retrieve、Generate Answer、Final Response。
- `app/static/styles.css`：新增紧凑时间线样式，展示 source rank、score、rerank、content chars、expanded chars、LLM usage 和耗时。

验证：

- 浏览器打开 `http://127.0.0.1:8000/` 无控制台错误。
- 页面查询“远程办公需要提前多久申请？”后，Trace 展示 Overview、Generate Answer、Total Tokens、Cache Hit Tokens。

## 2026-07-21：新增轻量本地检索工具模式

决策：

> 在完整开发版之外，新增一个可发给他人试用的轻量本地检索问答模式。

边界：

- 不使用业务数据库。
- 不保存 trace。
- 不保存查询历史。
- 不加载 embedding 模型。
- 不加载 reranker。
- 只读取用户指定目录。
- 只在用户本地生成索引缓存 `data/lite_index/`。
- 如启用 LLM，只把检索出来的 top sources 和用户问题发送给用户配置的 LLM API。

实现：

- `app/lite/indexer.py`：扫描授权目录，抽取 `.md`、`.txt`、`.pdf`，切分后写入 `chunks.jsonl` 和 `manifest.json`。
- `app/lite/search.py`：纯 Python 关键词检索，支持中文字符和 2-4 字 ngram。
- `app/lite/generator.py`：可选 LLM 汇总；无 LLM 时返回检索片段。
- `app/lite/main.py`：轻量 FastAPI 服务。
- `app/lite/static/`：独立轻量查询页面。
- `scripts/lite_index.py`：构建轻量索引。
- `scripts/lite_query.py`：CLI 查询。
- `scripts/run_lite.py`：启动轻量 Web，默认 `http://127.0.0.1:8010/`。

验证：

- `python scripts/lite_index.py --source-dir demo_documents`
- 结果：`Files=10`，`Chunks=10`。
- `python scripts/lite_query.py "远程办公需要提前多久申请？" --no-llm`
- 结果：`demo_remote_work_policy.md` 排名第 1。
- 浏览器打开 `http://127.0.0.1:8010/`，关闭 Use LLM 后查询同一问题，页面正常返回答案和 sources，控制台无错误。

结论：

- 轻量模式已经可以作为“低内存、本地目录检索 + 可选 LLM 汇总”的 MVP。
- 完整开发版仍保留数据库、Trace、评估、reranker，用于开发和质量诊断。

## 2026-07-21：轻量页面中文化与引用 sources 对齐

决策：

> 轻量 Web 面向普通用户，页面文案统一使用中文；选择文件后自动构建索引；引用来源数量与答案中的引用编号对齐。

实现：

- `app/lite/static/index.html`：标题、按钮、状态、字段名全部改为中文。
- `app/lite/static/app.js`：查询状态、索引结果、source 元信息中文化。
- `app/lite/static/index.html`：新增“选择文件”和“选择文件夹”，移除普通用户可见的“构建索引”按钮和开发目录输入。
- `app/lite/static/app.js`：选择本地文件后自动通过 multipart 上传给本地服务构建索引，并支持多次添加文件合并到当前索引。
- `app/lite/main.py`：新增 `/api/lite/index/upload`，直接从上传文件内容构建本地索引，不复制保存原文档。
- `app/lite/main.py`：解析答案中的 `[1]`、`[2]` 等引用编号，只返回对应 sources。
- 若答案没有可解析引用，则保留原检索 sources，避免无 LLM 模式下没有来源可看。

验证：

- 页面打开 `http://127.0.0.1:8010/`，标题为“本地知识库轻量工具”。
- 点击“选择文件”添加 `demo_remote_work_policy.md` 和 `demo_finance_reimbursement.md` 后自动构建索引，返回 `2 个文件，2 个片段`。
- 查询“远程办公需要提前多久申请？”，页面中文显示答案与引用来源。
- 控制台无错误。

## 2026-08-02：XLSX 保持只读流式解析并按需读取公式缓存

决策：

> XLSX 主工作簿始终使用 `read_only=True`。先流式扫描工作表 XML 获取合并区域
> 和公式存在性；只有检测到公式时，才额外打开 `data_only=True` 的只读工作簿。

原因：

- 普通大表不承担双工作簿遍历成本。
- 有公式时可以同时保留公式文本和缓存值。
- 缓存缺失不会再与真实空单元格混淆。
- 不切换到普通模式加载完整工作簿，保持 20 万行场景的内存边界。

验证：

- 20 万行 XLSX 峰值 RSS 增量为 28.68 MiB。
- 含缓存公式、无缓存公式、合并区域、日期、百分比和数字格式固定用例全部通过。

## 2026-08-04：索引提交使用可恢复事务，增量以内容指纹为入口

决策：

> 使用源文件 SHA-256 判断文档是否变化。所有索引文件继续通过同一事务提交，
> 并使用磁盘事务日志支持进程硬中断后的自动恢复。

实现：

- 未变化文件不进入解析器。
- 修改文件先完整写入独立暂存 JSONL，解析成功后才参与提交。
- 单个文件失败时保留其旧版本，其他成功文件可继续提交。
- 提交前写入 `.index-transaction.json`；下一次读取索引时自动判断提交完成或回滚。
- Embedding 缓存按 Chunk 键复用未变化向量。
- BM25 和 Embedding 缓存损坏只导致缓存重建，不使核心索引失效。

后续完成：

- 三类 JSONL 已升级为按文档 shard；修改和删除只替换目标 shard 与 manifest。
- 旧单体索引可与 shard 混合读取，并在文档变化时渐进迁移。
- Embedding 文档缓存不包含 `top_k`，因为 `top_k` 不影响文档向量。
- 查询结果缓存已把索引指纹、模式、模型、参数、知识库范围和 `top_k`
  全部纳入键，并限制为 128 项内存缓存。
