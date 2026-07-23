# Project Memory

更新时间：2026-07-16

## 为什么新建这个项目

用户原先在 `investment_agent` 中做智能投研系统，已经实现：

- LLM 客户端基建。
- LLM 生成 Black-Litterman 观点。
- LLM 抽取宏观因子。
- LLM 生成投研报告。
- Tool Calling 编排。

但经过讨论，发现如果长期押注“金融投资 Agent”，会遇到几个现实问题：

- 金融数据获取困难，尤其是高质量股债、大类资产、宏观和实时数据。
- 用户不是金融投资专业背景，长期深入该方向学习成本高。
- 金融投资涉及专业、合规、信任和结果验证壁垒。
- 投资收益本身不适合作为项目成败的主要衡量标准。

因此建议调整主线：

> 从“智能投研 Agent”转为“企业知识库 + RAG 评估 + Agent Trace”，金融投研只保留为第一个垂直样板。

这样更符合：

- 市场需求：企业知识库、RAG、内部文档问答、报告自动化更普遍。
- 项目基础：同级 `membrain` 已经有 RAG、文档管理、Trace、FastAPI 等基础。
- 可展示性：问答、引用、评估指标、Trace 页面都能快速形成可见结果。
- 可持续性：可以每周新增评估集、优化指标、接入新业务文档，不容易陷入空转。

## 当前战略判断

最优方向：

> 做一个可评估、可追踪、可展示的企业知识工作台。

不是单纯学习 RAG，也不是继续堆 Agent 框架，而是每个阶段都要有可见或可量化产出。

## 推荐产品形态

名称暂定：Enterprise Knowledge Agent

核心页面：

1. 文档管理：上传、列表、分块状态。
2. 知识库问答：问题、答案、引用来源。
3. 评估面板：Recall@5、引用命中率、完整度、幻觉率、延迟。
4. Agent Trace：展示 query 改写、检索、rerank、引用选择、生成、自检全过程。
5. 业务样板：金融投研报告生成。

## 资源决策

短期不需要云服务器，不需要 AutoDL，不需要 GPU。

当前阶段资源建议：

- 本机开发。
- LLM 走 API。
- 向量库先用 FAISS。
- 数据库先用 SQLite。
- 前端可先用 Streamlit 或简单 HTML。
- 等需要对外演示或定时任务时，再考虑轻量云服务器。

暂不建议：

- 本地部署大模型。
- 租 GPU。
- 做实时交易。
- 做自动投资决策。

## 数据决策

金融数据获取困难会影响 `investment_agent` 做成专业投资系统，但不影响新主线。

新主线的数据来源可以是：

- 本地 PDF / Markdown / TXT 文档。
- 企业制度、产品手册、客服 QA、课程资料、投研材料。
- 自建小型 demo 文档集。

金融投研只作为样板，不要求真实投资闭环。

## 同级项目记忆

### `investment_agent`

当前投研项目，已有模块：

- `config.py`
- `data_loader.py`
- `regime_incremental.py`
- `regime_classifier.py`
- `risk_metrics.py`
- `strategies/bl.py`
- `strategies/risk_parity.py`
- `strategies/momentum.py`
- `backtest.py`
- `llm_client.py`
- `llm_bl_views.py`
- `llm_factors.py`
- `llm_report.py`
- `agent.py`
- `main.py`

它后续角色是“金融投研样板插件”，不再作为唯一主线。

### `membrain`

最重要参考项目。

已有能力：

- FastAPI。
- Streamlit。
- LangGraph Agent。
- RAG 管线。
- FAISS。
- Reranker。
- HyDE。
- Neo4j 知识图谱。
- 文档管理。
- Agent Trace。
- pytest 测试。

后续优先从 `membrain` 借鉴或迁移最小可用模块，而不是重新造全套。

### `deepseek_agent`

只保留为学习记录和 FastAPI/LLM 服务参考。

已知学习记录：

- 主项目是 `llm_backend`。
- `ReAct_AI_Agent` 是独立示例，不是主运行链路。
- 前端源码不在仓库，只有构建产物。
- 学习进度停在 `llm_backend/main.py` 和 `app/core/*` 阶段。

## 协作原则

用户希望避免枯燥学习，所以每个阶段必须有产出：

- 可打开页面。
- 可截图。
- 可运行脚本。
- 可量化指标。
- 可保存报告。

不要只做理论解释。

每次推进时优先回答：

1. 这一步会产出什么？
2. 怎么验收？
3. 文件放在哪里？
4. 下一次打开项目该从哪里继续？

