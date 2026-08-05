# Documentation

项目文档以“单一进度源 + 稳定专题文档”为原则维护。

## 新会话入口

继续开发时只需先读 `DEVELOPMENT_PLAN.md`。它包含当前阶段、完成状态、验收门槛和后续任务，是唯一的开发进度来源。

确定本次任务后，再读取相关代码、测试和下方对应的专题文档。

## 文档职责

| 文件 | 职责 |
|---|---|
| `DEVELOPMENT_PLAN.md` | P0/P1/P2 路线、当前进度和待办清单 |
| `ACCEPTANCE_REPORTS.md` | 阶段退出验收记录 |
| `DEVELOPMENT_GUIDE.md` | Python 环境、常用命令和开发约定 |
| `BUILD_AND_RELEASE.md` | Windows/macOS 桌面版和轻量版构建发布 |
| `DECISIONS.md` | 已做出的重要技术与产品决策，只追加不重写 |
| `DOCUMENT_NODE.md` | DocumentNode、统一解析器和结构索引设计 |
| `PDF_PARSING_FLOW.md` | PDF v3 解析主流程、条件分支、回退和节点产物 |

评估资产和数据集说明位于 `evals/README.md`。

## 维护规则

- 阶段状态只更新 `DEVELOPMENT_PLAN.md`，不要再创建 `ROADMAP` 或 `NEXT_STEPS` 类重复进度文档。
- 阶段完成后，把可重复验证的结果追加到 `ACCEPTANCE_REPORTS.md`。
- 新的重要取舍追加到 `DECISIONS.md`。
- 命令、环境或发布方式变化时修改对应专题文档，不在进度清单里复制完整说明。
- `outputs/` 下的报告是本地生成物，文档只记录稳定指标和报告命名规则。
