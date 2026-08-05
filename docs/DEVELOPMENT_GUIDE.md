# Development Guide

## Python 环境

项目只支持 Python 3.11。不要直接使用系统 `python`，当前机器的系统命令可能指向不受支持的 Anaconda Python。

Windows 完整后端：

```powershell
.\scripts\install.ps1
.\scripts\run_web.ps1
```

Windows 桌面应用：

```powershell
.\scripts\install_desktop.ps1
.\scripts\run_desktop.ps1
```

虚拟环境职责：

| 环境 | 用途 |
|---|---|
| `.venv` | FastAPI、数据库、FAISS 和完整 RAG |
| `.venv-desktop` | PySide6 桌面应用、轻量索引和 P0 回归 |

版本约束同时记录在 `.python-version`、`pyproject.toml` 和 `app/__init__.py`。安装、运行和构建脚本也会拒绝非 Python 3.11 环境。

## 常用入口

完整 Web 工作台：

```powershell
.\scripts\run_web.ps1
```

地址：`http://127.0.0.1:8000/`

桌面应用：

```powershell
.\scripts\run_desktop.ps1
```

轻量 Web：

```powershell
.\.venv\Scripts\python.exe scripts\run_lite.py
```

地址：`http://127.0.0.1:8010/`

轻量索引和 CLI 查询：

```powershell
.\.venv-desktop\Scripts\python.exe scripts\lite_index.py --source-dir demo_documents
.\.venv-desktop\Scripts\python.exe scripts\lite_query.py "远程办公需要提前多久申请？" --no-llm
```

## 测试与静态检查

仓库测试基于标准库 `unittest`，不要求运行环境安装 `pytest`。

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -p "test_*.py" -v
.\.venv-desktop\Scripts\python.exe -m unittest discover -s tests -p "test_*.py" -v
.\.venv\Scripts\python.exe -m compileall -q app tests scripts
.\.venv-desktop\Scripts\python.exe -m compileall -q app tests scripts
```

桌面启动冒烟：

```powershell
$env:QT_QPA_PLATFORM = "offscreen"
.\.venv-desktop\Scripts\python.exe scripts\run_desktop.py --smoke-test
```

## 评估

冻结的 P0-1 回归：

```powershell
.\.venv-desktop\Scripts\python.exe scripts\eval_p0_1.py
```

完整后端检索和答案评估：

```powershell
.\.venv\Scripts\python.exe scripts\eval_retrieval.py --top-k 5
.\.venv\Scripts\python.exe scripts\eval_rag.py --no-llm --no-reranker --limit 2
```

Demo 企业制度集：

```powershell
.\.venv\Scripts\python.exe scripts\load_demo_documents.py
.\.venv\Scripts\python.exe scripts\load_demo_documents.py --load
.\.venv\Scripts\python.exe scripts\load_demo_documents.py --load --force
```

数据集、质量门槛和报告格式见 `evals/README.md`。

## 开发流程

1. 先读 `docs/DEVELOPMENT_PLAN.md`，确认当前阶段和退出门槛。
2. 修改前检查 `git status --short`，保留现有未提交改动。
3. 让测试范围与改动风险匹配；公共结构、索引格式和用户链路必须跑全量测试。
4. 质量相关改动必须执行冻结基线，不能只用单个案例证明。
5. 完成阶段后更新 `DEVELOPMENT_PLAN.md` 和 `ACCEPTANCE_REPORTS.md`。
6. 新的重要取舍追加到 `DECISIONS.md`。

## 数据与密钥

- 不提交 `data/`、`outputs/`、`.env*`、用户文档、索引和数据库。
- 不把 API Key 写入源码、默认配置或发布包。
- API Key 通过 `app/security/credentials.py` 保存到系统凭据库
  （Windows Credential Manager / macOS Keychain），不写入 QSettings 明文。
- 完全离线模式默认开启；`app/lite/remote_retrieval.set_remote_access(False)`
  禁止一切远程请求。错误消息经 `redact_secrets()` 脱敏。
- 真实目录测试只读取用户明确授权的位置。
- 远程 LLM、Embedding 或 Reranker 只能发送明确展示给用户的检索片段和问题，
  首次调用前需要用户确认数据范围。
