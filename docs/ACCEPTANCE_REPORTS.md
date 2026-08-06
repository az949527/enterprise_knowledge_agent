# Acceptance Reports

本文件记录已经完成阶段的退出验收。当前阶段状态和后续任务以 `DEVELOPMENT_PLAN.md` 为准。

## P0-3 Unified Parsing And Desktop Indexing

验收日期：2026-07-29

结论：P0-3 通过退出验收。P0-4、P0-5、P0-6 和 P0-7 按计划继续。

### 退出条件

| 条件 | 结果 | 证据 |
|---|---|---|
| DocumentNode、解析器和索引管线测试通过 | 通过 | 桌面环境 47 项全部通过；后端环境 44 项通过，1 项桌面专属测试因无 PySide6 跳过 |
| TXT/MD、PDF、CSV、XLSX 固定夹具 | 通过 | `tests/test_p0_3_acceptance.py` 混合格式端到端用例 |
| 桌面单文件选择器覆盖全部格式 | 通过 | 过滤器覆盖 `.txt`、`.md`、`.pdf`、`.csv`、`.xlsx` |
| 新旧索引格式行为明确 | 通过 | 旧版本、损坏 manifest/JSONL 提示重建；损坏 BM25 缓存自动重建 |
| 索引失败不覆盖旧索引 | 通过 | 解析中断和多文件提交中断逐字节验证旧索引保持一致 |
| 查询透传结构引用 | 通过 | PDF 页码/bbox、CSV 行列、XLSX Sheet/行号和 `source_anchor` 均返回 |
| 30 条普通文本基线不下降 | 通过 | 四项质量指标变化均为 `0` |

### 质量回归

命令：

```powershell
.\.venv-desktop\Scripts\python.exe scripts\eval_p0_1.py
```

| 指标 | 基线 | 当前 | 变化 |
|---|---:|---:|---:|
| Recall@5 | 1.000 | 1.000 | 0.000 |
| MRR | 1.000 | 1.000 | 0.000 |
| 答案覆盖率 | 1.000 | 1.000 | 0.000 |
| 引用准确率 | 0.200 | 0.200 | 0.000 |

本地报告命名为 `outputs/evals/p0_1_eval_*.json` 和对应 Markdown；`outputs/` 不提交版本库。

### 大文件内存

固定行式文本分别为 5 MiB 和 20 MiB，使用独立 Python 3.11 子进程执行桌面同源 `build_index()`。

| 输入 | 峰值 RSS | 索引阶段 RSS 增量 | 节点 | Chunk | 耗时 |
|---|---:|---:|---:|---:|---:|
| 5 MiB | 35.004 MiB | 0.559 MiB | 1,281 | 7,682 | 0.69 s |
| 20 MiB | 36.262 MiB | 1.777 MiB | 5,122 | 30,728 | 3.81 s |

输入扩大 4 倍时，进程峰值 RSS 仅增加约 1.26 MiB。桌面 `IndexWorker` 的 Qt 事件循环响应测试也通过。

### 可靠性边界

P0-3 提供临时文件、异常清理和提交失败回滚。解析失败或受控提交失败不会改变旧索引。

进程被强制终止后的跨进程恢复、取消按钮、增量更新和失败文档隔离属于 P0-7。

## P0-4 Domain-Neutral Retrieval Signals

验收日期：2026-07-30

结论：P0-4 通过退出验收。当前进入 P0-5 PDF 结构化解析和 P0-6 CSV/XLSX 基础检索。

### 代码边界

- `app/retrieval_signals.py` 统一提供摘要、流程、数量、时间和低信息噪声信号。
- `app/lite/search.py` 和 `app/lite/bm25_search.py` 不再包含腌制领域规则。
- `app/rag/reranker.py` 不再包含近红外、PLSR、预处理或成熟度论文词表。
- 代码中不提供默认领域词典，未引入 TF-IDF 或隐式术语加权。

### 质量对照

P0-1 冻结的 30 条企业制度基线：

| 指标 | 改造前 | 当前 | 变化 |
|---|---:|---:|---:|
| Recall@5 | 1.000 | 1.000 | 0.000 |
| MRR | 1.000 | 1.000 | 0.000 |
| 答案覆盖率 | 1.000 | 1.000 | 0.000 |
| 引用准确率 | 0.200 | 0.200 | 0.000 |

P0-4 固定跨领域排序集：

| 指标 | 硬编码规则基线 | 通用信号 | 变化 |
|---|---:|---:|---:|
| Lite Top-1 | 0.500 | 1.000 | +0.500 |
| Lite MRR | 0.750 | 1.000 | +0.250 |
| Reranker Top-1 | 0.250 | 1.000 | +0.750 |
| Reranker MRR | 0.625 | 1.000 | +0.375 |

固定集覆盖制造、IT、采购、财务、法务、HR、运营和行政领域。数据集 SHA-256 为
`ed3c11d827d6c85f8ca9fb1fabcebd74c9ba4a0a818c7b0cdef3d167a197a033`。

### 验证

```powershell
.\.venv-desktop\Scripts\python.exe -m unittest discover -s tests -p "test_*.py" -v
.\.venv-desktop\Scripts\python.exe scripts\eval_p0_1.py
.\.venv-desktop\Scripts\python.exe scripts\eval_p0_4.py
```

桌面环境 57 项测试通过。后端环境 55 项通过，1 项桌面专属测试因无 PySide6 跳过。
P0-4 通用信号的平均评分耗时低于 1 ms。

## P0-5 PDF Structure Optimization v3

验收日期：2026-07-31

结论：P0-5 已达到当前阶段收口门槛。OCR、pdfplumber A/B、任意复杂 PDF
泛化和完整公式还原明确延期，不阻塞 P0-6。

### 实际代码产出

- 标题排除纯数字、公式、DOI、图题、表题和长句。
- 多栏重排要求持续平行栏、明确栏间距和垂直重叠，低置信度保留 PyMuPDF 原顺序。
- 新增三线表检测；兼容的相邻页表格合并为同一个 table 节点并移除重复表头。
- 多层表头前向填充父级，生成 `训练集.Rc`、`预测集.RMSEP` 等完整列名。
- 数学字体和运算式按空间邻接聚合为 `content_kind=formula` 文本节点。
- figure 节点保存图题、页面 bbox、视觉 bbox、图题 bbox 和附近正文。
- 首次页面扫描缓存文本、绘图和图像区域；只有疑似表格页调用 `find_tables()`。

### 固定验收

```powershell
.\.venv-desktop\Scripts\python.exe -m unittest tests.test_p0_5_acceptance -v
.\.venv-desktop\Scripts\python.exe scripts\eval_p0_5.py
```

- P0-5 专项测试：11/11 通过。
- 完整桌面测试：73/73 通过。
- P0-1：Recall@5、MRR、答案覆盖率均为 1.000，引用准确率为 0.200。
- P0-4：Lite 和本地 reranker 的 Top-1、MRR 均为 1.000。

### 真实 PDF 对比

13 页真实论文，5 次重复运行：

| 指标 | 改造前 | PDF v3 | 变化 |
|---|---:|---:|---:|
| 历史解析中位数 | 1.4154 s | 1.6050 s | 机器负载波动 |
| `find_tables()` 调用页数 | 8/13 | 5/13 | -37.5% |
| 同进程交替强制全页 A/B | 2.6136 s（13/13） | 1.6050 s（5/13） | -38.6% |
| text 节点 | 42 | 46 | 过滤标题噪声和公式碎片后收敛 |
| table 节点 | 6 | 5 | 2 组跨页续表合并 |
| figure 节点 | 17 | 8 | 双语图题去重 |
| 公式区域节点 | 未独立保存 | 3 | 仅保留完整数学区域 |

机器可读结果位于 `outputs/evals/p0_5_eval_20260731_151926.json`，
对应 Markdown 为 `outputs/evals/p0_5_eval_20260731_151926.md`。

### P0-5 收口复核

复核日期：2026-07-31

- 真实 PDF：13 页。
- 逻辑 table：5 个，其中跨页合并 2 个。
- figure：8 个，全部为 image-backed，无 caption-only 重复节点。
- 公式区域：3 个。
- 标题：25 个，作者行、英文副标题和图例不再污染章节路径。
- 结构化检索抽查：5 条，Recall@3=`1.000`，Top-1=`0.800`。
- 当前完整测试：73 项全部通过。
- 下一阶段：P0-6 CSV/XLSX 基础检索。

## P0-6 CSV/XLSX Reliable Retrieval

验收日期：2026-08-02

文档收口日期：2026-08-04

结论：P0-6 通过退出验收，下一阶段进入 P0-7 索引可靠性和增量更新。

### 实际代码产出

- CSV 支持 UTF-8、UTF-8 BOM、GB18030 和逗号、分号、Tab、竖线分隔符。
- 自动编码确认失败时，桌面端允许用户选择编码并对同一批文件重试。
- CSV 异常列数会报告文件、原始行号、预期列数和实际列数。
- XLSX 保持 `openpyxl` 只读流式解析，保存合并区域、Sheet、行号和列信息。
- 日期使用 ISO 格式，百分比和数字按单元格格式标准化。
- 公式文本、缓存值、缺失缓存和真实空值可区分；缺少缓存时在检索文本中明确提示。
- Sheet 摘要记录列类型、关键列、指标列、数据范围和公式状态。
- `csv_parser_v2`、`xlsx_parser_v2` 会使旧 CSV/XLSX 索引明确提示重建。

### 固定验收

```powershell
.\.venv-desktop\Scripts\python.exe -m unittest discover -s tests -p "test_*.py" -v
.\.venv-desktop\Scripts\python.exe scripts\eval_p0_6.py
```

- P0-6 专项测试：6/6 通过。
- 完整桌面测试：80/80 通过。
- CSV/XLSX 行组可被 BM25 检索，并透传 Sheet、行号、列范围和原始行号。
- P0-1：Recall@5、MRR、答案覆盖率均为 1.000，引用准确率为 0.200。
- P0-4：Lite 和本地 reranker 的 Top-1、MRR 均为 1.000。
- P0-5：11 项专项测试通过，13 页真实 PDF 的表格门控保持 5 次调用。

### 大文件内存

| 输入 | 文件大小 | 节点 | 耗时 | 峰值 RSS 增量 |
|---|---:|---:|---:|---:|
| CSV | 50.17 MiB | 17,201 | 1.89 s | 2.14 MiB |
| XLSX | 20 万行，5.05 MiB | 4,001 | 25.74 s | 28.68 MiB |

有效机器可读报告为 `outputs/evals/p0_6_eval_20260802_230252.json`，
对应 Markdown 为 `outputs/evals/p0_6_eval_20260802_230252.md`。

`p0_6_eval_20260802_230038` 的 Windows RSS 回退记录为 0，不作为验收证据；
采样函数修复后重新生成了上述有效报告。

## P0-7 Index Reliability And Incremental Updates

验收日期：2026-08-04

结论：P0-7 通过退出验收，下一阶段进入 P0-8 数据安全和离线边界。

### 实际代码产出

- manifest 文档记录保存源文件 SHA-256、文件大小和修改时间。
- 未变化文件在解析前跳过；目录同步支持新增、修改和删除。
- 修改文件解析失败时保留旧节点，其他成功文件仍可原子提交。
- `.index-transaction.json` 记录多文件提交；硬中断后下一次启动自动恢复。
- 索引 manifest 保存完整 Chunk 指纹。
- 诊断覆盖 manifest、nodes、parents、chunks、引用链、BM25 SQLite 和 Embedding 缓存。
- BM25/Embedding 缓存损坏标记为可重建降级，不使核心知识库不可用。
- Embedding 缓存升级为 v2，只请求新增或变化 Chunk 的向量。
- 查询结果使用 128 项有界内存缓存，键包含索引指纹、查询、模式、模型、
  参数、知识库范围和 `top_k`。
- nodes、parents 和 chunks 使用按文档 shard 存储。
- 修改、删除和公共追加只替换相关 shard 与顶层 manifest。
- 旧单体 JSONL 索引可混合读取，并在文档修改时渐进迁移。
- 桌面端增加进度、取消、诊断和从文件夹重建入口。
- 公共 API 支持索引诊断和 `force_rebuild`。
- 加密 PDF 和损坏/加密 XLSX 返回明确失败原因。

### 固定验收

```powershell
.\.venv-desktop\Scripts\python.exe -m unittest discover -s tests -p "test_*.py" -v
.\.venv-desktop\Scripts\python.exe scripts\eval_p0_7.py
```

- P0-7 专项测试：12/12 通过。
- 完整桌面测试：95/95 通过。
- 桌面离屏启动通过。
- P0-1、P0-4、P0-5、P0-6 回归全部通过。

### 增量构建对比

固定输入为 10 MiB 文本和一个小型可变文档：

| 场景 | 耗时 | 新增 | 更新 | 跳过 |
|---|---:|---:|---:|---:|
| 首次构建 | 1.537 s | 2 | 0 | 0 |
| 完全未变化 | 0.041 s | 0 | 0 | 2 |
| 修改一个小文件 | 0.366 s | 0 | 1 | 1 |

未变化同步不解析文档，也不重建 Embedding。单文件修改只重新解析、嵌入并
替换目标 shard；未变化 shard 逐字节保持一致。

有效报告为 `outputs/evals/p0_7_eval_20260804_142608.json` 和对应 Markdown。

## P0-8 Data Security And Offline Boundary

验收日期：2026-08-04

结论：P0-8 通过退出验收，下一阶段进入 P1 核心能力增强。

### 实际代码产出

- 新增 `app/security/credentials.py`：跨平台系统凭据库。
  Windows 用 ctypes 调 Credential Manager，macOS 用 `security` CLI，
  其他平台用本地混淆文件兜底，不引入新依赖。
- 新增 `app/security/redaction.py`：`redact_secrets()` 统一脱敏
  API Key，日志和错误信息不记录密钥。
- `app/lite/remote_retrieval.py` 增加模块级离线门禁
  `set_remote_access(False)`，完全离线时禁止一切远程 LLM、
  Embedding 和 Reranker 请求，并对错误消息脱敏。
- `app/lite/desktop_query.py` 支持 `offline` 参数，离线时短路返回
  `mode=offline`，不产生任何网络请求。
- 桌面设置页新增"完全离线模式"总控，默认开启；开启时禁用远程功能复选。
- API Key 从 QSettings 明文迁移到系统凭据库；`SETTINGS_SCHEMA_VERSION`
  升到 3，旧明文键启动时读入凭据库并删除。
- 首次远程调用前弹出确认框，显示问题内容、将发送的文档片段范围和目标服务
  地址，支持"同意并记住"（按 Base URL 记忆）或"仅本次同意"。
- 对话页新增联网状态指示；应用临时目录在启动和退出时清理。
- 本阶段不新增遥测，远程调用只发送明确展示的问题与检索片段。

### 固定验收

```powershell
.\.venv-desktop\Scripts\python.exe -m unittest discover -s tests -p "test_*.py" -v
.\.venv-desktop\Scripts\python.exe scripts\eval_p0_8.py
```

- P0-8 专项测试：25/25 通过（离线门禁、桌面短路、凭据读写与真实
  Windows Credential Manager round-trip、QSettings 迁移、脱敏、
  桌面离线 UI、授权确认、临时目录清理）。
- 完整桌面测试：120/120 通过。
- 桌面离屏启动通过。
- P0-1 冻结基线无下降：Recall@5、MRR、答案覆盖率均为 1.000，
  引用准确率为 0.200。

### 安全验证

| 项 | 结果 |
|---|---|
| 完全离线门禁 | `set_remote_access(False)` 后 Embedding/Reranker 抛 `RemoteModelError`，恢复后在线 |
| Windows Credential Manager | 唯一账户写入 → 读取 → 删除 round-trip 通过 |
| QSettings 明文迁移 | 旧 `llm/api_key`、`retrieval/api_key` 读入凭据库并从设置删除 |
| API Key 脱敏 | `Authorization: Bearer sk-...`、`api_key=...` 均被替换为 `[REDACTED]` |
| 离线 UI | 离线默认开启，禁用远程复选；联网查询时显示联网指示 |

有效机器可读报告为 `outputs/evals/p0_8_eval_20260804_172429.json`，
对应 Markdown 为 `outputs/evals/p0_8_eval_20260804_172429.md`。

## Excel Multi-document Query Optimization

验收日期：2026-08-05

结论：完成查询路由、文件名检索、工作簿摘要和多文档来源覆盖；本轮未引入
GraphRAG 或新的大型依赖。

### 实际代码产出

- `xlsx_parser_v3` 新增 `workbook_summary`，形成
  `workbook_summary -> sheet_summary -> row_group` 层级。
- 查询时从 Chunk 结构字段派生统一 `search_text`，包含文件名、文件主名、
  文件类型、Sheet、节点类型和字段；BM25、Embedding、Reranker 使用同一
  检索文本，但不在每个 Chunk 中重复存储。
- “几个/多少个 Excel”和文件列表问题直接读取 manifest，不发起远程请求。
- 指定文件名时先限定 `source_path`，再执行内容检索。
- “我的 Excel 表有什么”“表里有什么内容”等问题直接选择每个工作簿摘要，
  不允许单个工作簿占满 Top-K。

### 验证结果

- 完整测试：139/139 通过。
- 双 Excel + PDF 固定场景：5/5 通过。
- 真实 `20230526.xlsx`、`final_df.xlsx` 与旧 PDF 节点临时重建：
  145 -> 147 Chunk，仅新增两个工作簿摘要。
- 清单查询约 6 ms；双工作簿概览约 15-37 ms，均不调用远程检索。
- 首轮实现曾将 `search_text` 重复写入 Chunk，实测核心 JSON 增加约 22%；
  最终实现改为查询时派生。真实混合索引最终从 650,748 增至
  652,028 字节，仅增加 1,280 字节（约 0.2%）。
- 最终真实数据临时重建：解析约 1.21 秒，索引写入约 0.16 秒，
  Python 堆峰值约 7.2 MiB。
- 完全本地查询：Excel 数量约 5.4 ms，指定工作簿概览约 13.6 ms，
  双工作簿概览约 13.7-35.4 ms。
- P0-7 最终性能：10 MiB 初建 1.514 秒、未变化同步 0.038 秒、
  单文件更新 0.329 秒，诊断状态 `healthy`。
- P0-6 20 万行 XLSX：4,002 个节点，RSS 增量约 27.08 MiB，
  未高于优化前验收结果。

## P1-1 Parent-Child 自适应检索

验收日期：2026-08-06

结论：父上下文扩展已接线并达到当前阶段收口；冻结基线上开/关对照 delta=0
（语料为短文档，chunks 与父内容相同触发去重），质量回退门禁通过。

### 实际代码产出

- 新增 `app/lite/parent_context.py`：分片感知、按需懒加载的
  `ParentContextResolver`，给定检索命中的 sources（每条带 `document_id` +
  `parent_id`），只读命中文档对应分片的 nodes/parents，附加父节点内容。
  - 表格父节点用 `effective_display_content`（渲染后 Markdown）。
  - 单父上限 `PARENT_CONTEXT_MAX_PARENT_CHARS` 与累计上限
    `PARENT_CONTEXT_MAX_TOTAL_CHARS` 兜底。
  - 命中块内容与父内容相同时去重，不重复送生成。
  - 缺 parent_id / 缺文档 / 缺父记录时优雅降级。
- `app/lite/desktop_query.py`：`query_desktop_index` 增加 `use_parent_context`
  （默认读 settings），在 `answer_query` 前、缓存读写之后扩展来源；
  缓存仍存精简 child，父上下文每次查询现算。仅 content 意图生效。
- `app/lite/generator.py`：`build_context` 把父上下文与“命中的具体片段”
  标记一起送入生成提示；child 的 `content`/`chunk_index` 不变，
  引用回溯与抽取式回答不受影响。
- `app/core/config.py`：新增 `PARENT_CONTEXT_ENABLED`、
  `PARENT_CONTEXT_MAX_PARENT_CHARS`、`PARENT_CONTEXT_MAX_TOTAL_CHARS`。
- 新增 `scripts/eval_p1_1.py`：冻结 30 条基线上开/关对照评估，
  输出 JSON + Markdown 报告，并做与已提交基线的质量回退门禁。

### 固定验收

```powershell
.\.venv-desktop\Scripts\python.exe -m unittest tests.test_parent_context -v
.\.venv-desktop\Scripts\python.exe -m unittest discover -s tests
.\.venv-desktop\Scripts\python.exe scripts\eval_p1_1.py
```

- P1-1 专项测试：10/10 通过（单文档文本父、表格父渲染、单父/累计截断、
  自父去重、跨分片、缺父优雅降级、旧单体布局、CJK 截断、查询集成与上下文组装）。
- 完整桌面测试：149/149 通过（含原有 139）。
- 评估对照：no_parent 与 parent_context 在冻结基线上
  recall/mrr/answer_coverage/citation_accuracy 均无差异（delta=0），
  质量回退门禁通过（无回归）。
- 有效报告为 `outputs/evals/p1_1_eval_*.json` 和对应 Markdown。
