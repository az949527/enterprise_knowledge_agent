# Start Here

运行环境统一为 Python 3.11。Windows 首次进入项目先执行：

```powershell
.\scripts\install.ps1
.\scripts\install_desktop.ps1
```

不要直接使用可能指向 Anaconda Python 3.8 的系统 `python`。

新会话进入本项目时，先读这三个文件：

1. `memory/PROJECT_MEMORY.md`
2. `docs/ROADMAP.md`
3. `docs/NEXT_STEPS.md`

## 一句话上下文

本项目是从 `investment_agent` 拆出的新主线，目标是做“企业知识库 + RAG 评估 + Agent Trace”的可展示工作台；`investment_agent` 后续只作为金融投研样板，不再作为唯一主线。

## 当前下一步

先完成第一阶段闭环：

```text
demo_docs -> chunks -> FAISS index -> question -> answer with citations
```

优先做脚本，不急着做 UI。

建议第一批命令目标：

```bash
python scripts/build_index.py
python scripts/ask.py "根据文档回答一个问题"
```

## 给新会话助手的提示

如果用户说“继续这个项目”，不要重新讨论方向选择，直接读取上述记忆文件，然后从 `docs/NEXT_STEPS.md` 的第一阶段继续。
