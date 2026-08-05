# Evals

本目录保存可重复执行的 RAG 评估资产。

## Demo 企业制度语料

`demo_documents/` 包含 10 篇模拟企业制度和流程文档，用于没有真实业务数据时继续开发和回归。

加载到完整后端：

```powershell
.\.venv\Scripts\python.exe scripts\load_demo_documents.py --load
```

替换已加载的同名文档：

```powershell
.\.venv\Scripts\python.exe scripts\load_demo_documents.py --load --force
```

构建轻量索引并查询：

```powershell
.\.venv-desktop\Scripts\python.exe scripts\lite_index.py --source-dir demo_documents
.\.venv-desktop\Scripts\python.exe scripts\lite_query.py "远程办公需要提前多久申请？" --no-llm
```

对应冻结评估集为 `demo_enterprise_eval_dataset.json`。

## P0-1 回归基线

- `demo_enterprise_eval_dataset.json`：冻结的 30 条企业制度问题。
- `p0_1_baseline_manifest.json`：固定数据集、文档语料、指标和验收门槛。
- `p0_1_extension_cases.json`：普通文本、PDF 跨页、PDF 表格、XLSX 和拒答用例登记表。
- `baselines/p0_1_lite_bm25.json`：BM25 本地模式的机器可读基线。
- `baselines/p0_1_lite_bm25.md`：当前基线摘要。

冻结清单同时校验评估集和 `demo_documents` 的 SHA-256。任一内容发生变化时，评估直接失败，避免比较不同数据上的结果。

首次建立或经审核后重建基线：

```powershell
.\.venv-desktop\Scripts\python.exe scripts/eval_p0_1.py --write-baseline
```

执行日常回归：

```powershell
.\.venv-desktop\Scripts\python.exe scripts/eval_p0_1.py
```

基线文件存在时，日常回归自动生成改造前后对照并应用质量门槛。任何整体或同类型质量指标下降超过 `0.01` 都会返回退出码 `2`。

执行当前已就绪的扩展用例：

```powershell
.\.venv-desktop\Scripts\python.exe scripts/eval_p0_1.py --include-extensions
```

扩展集默认单独出报告，不与 30 条冻结基线比较。`fixture_pending` 用例只登记问题、预期证据和夹具路径，不会提前驱动尚未实施的 PDF 表格、跨页或 XLSX 功能。对应功能交付时，应补充固定夹具并把状态改为 `ready`。

LLM 模式如需记录费用，应传入每百万输入和输出 token 的美元价格：

```powershell
.\.venv-desktop\Scripts\python.exe scripts/eval_p0_1.py --use-llm `
  --llm-api-key $env:LLM_API_KEY `
  --llm-base-url $env:LLM_BASE_URL `
  --llm-model $env:LLM_MODEL `
  --input-cost-per-million 1.0 `
  --output-cost-per-million 2.0
```

报告记录 Recall@5、MRR、答案覆盖率、引用准确率、平均/P95 延迟、索引耗时、进程峰值 RSS、索引磁盘、API token 和费用。日常报告位于 `outputs/evals`，该目录不提交版本库。

## P0-4 领域扩展评估

- `p0_4_domain_extension_cases.json`：冻结的 8 条跨领域候选排序用例。
- `baselines/p0_4_domain_rules.json`：改造前领域硬编码规则的机器可读基线。
- `scripts/eval_p0_4.py`：当前通用检索信号的质量和延迟评估入口。

执行对照：

```powershell
.\.venv-desktop\Scripts\python.exe scripts\eval_p0_4.py
```

评估分别记录 Lite 规则评分和本地 reranker 的 Top-1、MRR、平均评分延迟。
数据集 SHA-256 不一致或任一质量指标下降超过 `0.01` 时，验收失败并返回退出码 `2`。

## P0-5 PDF 结构评估

`scripts/eval_p0_5.py` 会实际执行 P0-5 专项测试，并对 `data/documents` 下的真实
PDF 重复解析，记录解析器版本、节点产物、公式节点、跨页表格、耗时和
`find_tables()` 调用页数。

```powershell
.\.venv-desktop\Scripts\python.exe scripts\eval_p0_5.py
```

指定真实 PDF、运行次数和改造前耗时：

```powershell
.\.venv-desktop\Scripts\python.exe scripts\eval_p0_5.py `
  --pdf data\documents\sample.pdf `
  --runs 5 `
  --baseline-seconds 1.4154
```

报告同时执行当前门控模式和强制每页调用 `find_tables()` 的同进程 A/B，
并输出到 `outputs/evals/p0_5_eval_*.json` 和对应 Markdown。

## P0-6 CSV/XLSX 结构与内存评估

`scripts/eval_p0_6.py` 会运行 P0-6 专项测试，并生成 50 MiB CSV 和 20 万行
XLSX 固定夹具，在独立子进程中记录解析耗时、节点数量和峰值 RSS 增量。

```powershell
.\.venv-desktop\Scripts\python.exe scripts\eval_p0_6.py
```

开发阶段可缩小夹具规模：

```powershell
.\.venv-desktop\Scripts\python.exe scripts\eval_p0_6.py `
  --csv-mib 2 `
  --xlsx-rows 10000
```

报告输出到 `outputs/evals/p0_6_eval_*.json` 和对应 Markdown。正式验收要求
专项功能测试通过，且 CSV/XLSX 的 RSS 增量分别不超过脚本中的固定门槛。

## P0-7 增量索引与恢复评估

`scripts/eval_p0_7.py` 运行 P0-7 专项测试，并使用固定文本夹具比较首次构建、
完全未变化同步和单文件修改，同时执行索引诊断。

```powershell
.\.venv-desktop\Scripts\python.exe scripts\eval_p0_7.py
```

缩小固定夹具：

```powershell
.\.venv-desktop\Scripts\python.exe scripts\eval_p0_7.py --fixture-mib 2
```

报告记录新增、更新、删除、跳过、失败数量和各阶段耗时，输出到
`outputs/evals/p0_7_eval_*.json` 和对应 Markdown。
