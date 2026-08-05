# Start Here

项目统一使用 Python 3.11。Windows 首次进入项目：

```powershell
.\scripts\install.ps1
.\scripts\install_desktop.ps1
```

不要直接调用可能指向其他版本的系统 `python`。

## 新会话入口

继续开发时只要求先读：

1. `docs/DEVELOPMENT_PLAN.md`

它是唯一的阶段计划和进度来源。确认本次任务后，再读取相关代码、测试和必要的专题文档；不要从旧记录推断当前任务。

## 一句话上下文

本项目主线是可评估、可追踪的企业知识库 Agent，包含结构化文档解析、RAG 问答、引用定位、质量评估、Agent Trace 和桌面发行。

## 当前状态

P0-1 已建立冻结基线，P0-2 已完成 DocumentNode 主体结构，P0-3 已于 2026-07-29 通过统一解析和桌面索引退出验收，P0-4 已于 2026-07-30 通过领域硬编码清理验收。

当前 P0-5 PDF v3 已完成标题噪声过滤、置信度多栏重排、三线表、跨页表格、多层表头、公式区域、figure 节点和表格调用门控，并已通过真实 PDF 收口夹具、73 项完整测试和性能报告。当前阶段 P0-5 已收口，下一阶段进入 P0-6 CSV/XLSX 基础检索；OCR、pdfplumber A/B 和任意复杂 PDF 泛化列为延期项。具体退出条件直接读取 `docs/DEVELOPMENT_PLAN.md`。

## 给新会话助手的提示

如果用户说“继续这个项目”，先读取 `docs/DEVELOPMENT_PLAN.md`，确认最新 checklist 状态后直接继续。实际修改前仍需检查相关代码和测试，不重新设计已经确定的方向。
