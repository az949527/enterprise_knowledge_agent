# Evals

本目录保存可重复执行的 RAG 评估资产。

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
