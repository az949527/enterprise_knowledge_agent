# DocumentNode 结构层

`app/documents/node.py` 是桌面轻量链路和 Web 公共能力共享的文档中间结构。

## P0-2 字段

| 字段 | 说明 |
|---|---|
| `node_id` | 由文档、解析器版本、位置和内容确定性生成的节点 ID |
| `document_id` | 文档逻辑 ID |
| `content_hash` | 标准检索文本的 SHA-256 |
| `parser_version` | 产生节点的解析器版本 |
| `node_type` | `text`、`table`、`figure`、`sheet_summary`、`row_group` |
| `page_or_sheet` | PDF 页码或工作表名称 |
| `section_path` | 节点所在章节路径 |
| `sequence` | 节点在文档中的顺序 |
| `bbox` | 页面坐标 `[x0, y0, x1, y1]` |
| `row_start` / `row_end` | 表格行范围 |
| `column_start` / `column_end` | 表格列范围 |
| `parent_id` | 父章节、父表格或父节点 ID |
| `content` | 标准检索文本 |
| `display_content` | 与检索文本不同时才保存的展示文本 |
| `source_anchor` | 页码、Sheet、行列或章节等引用锚点 |
| `metadata` | 节点类型特有的扩展元数据 |

普通正文的 `display_content` 为 `null`，展示时使用 `effective_display_content` 回退到 `content`，避免保存两份相同文本。

项目统一使用 Python 3.11，`DocumentNode` 和 `BoundingBox` 采用 `dataclass(slots=True)`。

## 当前接入范围

- `app/lite/indexer.py`：TXT、Markdown 和现有 PDF 提取结果先生成 `DocumentNode`，再按原策略分块。
- `app/desktop/main.py`：桌面索引线程直接传递节点，不再传递纯文本元组。
- `app/lite/main.py`：轻量上传接口直接传递节点。
- `app/services/document_service.py`：Web 公共文档服务先生成节点，再进入现有分块和向量链路。
- BM25、轻量关键词检索和远程 Embedding 结果透传节点结构和 `source_anchor`。

为了保持 P0-1 基线，本次没有改变分块尺寸、chunk ID、检索算法或生成策略。

## P0-3 边界

本次不包含以下工作：

- 不建立统一解析器接口或解析器注册表。
- 不把 `DocumentNode` 和 Chunk 分别流式写入 SQLite/JSONL。
- 不建立父节点按需加载和 Parent-Child 检索。
- 不增加索引格式迁移、恢复或增量更新。
- 不合并 `app/lite` 和 `app/rag` 的两套解析入口。

这些内容在 P0-3 和 P0-7 中继续实施。

## P0-1 回归结果

本次结构改造前后：

- Recall@5：`1.0 -> 1.0`
- MRR：`1.0 -> 1.0`
- 答案覆盖率：`1.0 -> 1.0`
- 引用准确率：`0.2 -> 0.2`
- 索引峰值内存：增加约 `56 KiB`
- 索引磁盘：增加约 `8.8 KiB`

结构元数据没有改变当前检索与回答质量，磁盘增量来自节点 ID、哈希、解析器版本和引用锚点字段。
