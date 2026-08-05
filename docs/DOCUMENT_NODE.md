# DocumentNode 结构层

`app/documents/node.py` 是桌面轻量链路和 Web 公共能力共享的文档中间结构。

## P0-2 字段

| 字段 | 说明 |
|---|---|
| `node_id` | 由文档、解析器版本、位置和内容确定性生成的节点 ID |
| `document_id` | 文档逻辑 ID |
| `content_hash` | 标准检索文本的 SHA-256 |
| `parser_version` | 产生节点的解析器版本 |
| `node_type` | `text`、`table`、`figure`、`workbook_summary`、`sheet_summary`、`row_group` |
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

- `app/documents/parsers.py`：通过统一 `DocumentParser` 接口注册 TXT/MD、PDF、CSV 和 XLSX 解析器，均使用 Iterator/生成器输出节点。
- `app/documents/pdf_parser.py`：PDF v3 结构解析，负责置信度多栏顺序、章节路径、公式区域、figure、三线表、跨页表格、重复页眉页脚和扫描件诊断。
- `app/lite/indexer.py`：解析节点进入统一索引管线，再生成 Parent 映射和检索 Chunk。
- `app/desktop/main.py`：桌面索引线程直接传递节点，不再传递纯文本元组。
- `app/lite/main.py`：轻量上传接口直接传递节点。
- `app/services/document_service.py`：Web 公共文档服务先生成节点，再进入现有分块和向量链路。
- BM25、轻量关键词检索和远程 Embedding 结果透传节点结构和 `source_anchor`。

索引目录现在包含：

- `nodes.jsonl`：每个解析节点保存一次，包含标准检索内容和可选展示内容。
- `parents.jsonl`：`parent_id` 到内容节点的映射，不复制完整父节点文本。
- `chunks.jsonl`：检索子块保存原始 `content`、`parent_id` 和结构引用字段；
  包含文件名、类型和 Sheet 的 `search_text` 在查询时从结构字段派生，避免
  每个 Chunk 重复存储元数据。
- `manifest.json`：索引格式版本、解析器版本、节点类型和文件清单。

完整重建使用临时 JSONL 文件、清理保护和多文件提交回滚；解析器生成一个节点后即可继续写入，追加索引时旧节点也通过 JSONL 迭代器读取，不需要先构造整个知识库节点列表。为了保持 P0-1 基线，普通 Markdown 文档的 chunk 尺寸、chunk ID、检索算法和生成策略没有改变。

P0-3 已于 2026-07-29 通过退出验收，测试、质量回归和大文件内存数据见 `ACCEPTANCE_REPORTS.md`。

P0-5 当前使用 `pdf_parser_v3`。PDF 正文、表格和图分别生成 `text`、`table`、
`figure` 节点；公式区域使用带 `content_kind=formula` 的 `text` 节点整体保存。
表格不会再混入普通正文 Chunk；旧 PDF 解析器版本索引需要重建。

P0-6 当前使用 `csv_parser_v2`，XLSX 已在多文档查询优化中升级为
`xlsx_parser_v3`。XLSX 使用 `workbook_summary -> sheet_summary ->
row_group` 层级保存文件名、Sheet 清单、列名、原始行号、列范围、合并区域、
列类型和公式状态。XLSX 公式节点元数据同时区分公式文本、可用缓存值、
缺失缓存和真实空值；旧 XLSX 解析器版本索引需要重建。

P0-7 manifest 的每个文档记录增加 `source_sha256`、`source_size` 和
`source_mtime_ns`，用于在解析前识别未变化文档。节点结构本身不保存绝对
本地文件路径；增量同步继续通过标准 `source_path` 和 `document_id` 替换节点。

P0-7 完成后，每个文档的 `nodes.jsonl`、`parents.jsonl` 和 `chunks.jsonl`
保存在 manifest 指向的独立 shard 中。顶层同名 JSONL 仅作为旧索引兼容入口；
旧单体文档和新 shard 可以混合读取，文档被修改时自动迁移。

## P0-3 边界

本次仍不包含以下工作：

- 不把节点和 Chunk 同时写入 Web SQLite；桌面路径使用 JSONL。
- 不在查询生成链路中自动扩展同章节父节点。
- 不增加索引取消、进度展示、损坏恢复和真正的增量删除更新。
- Web 数据库 Chunk 表暂不保存完整结构锚点，桌面 JSONL 是本阶段主验收路径。

上述内容在 P0-7 和后续产品化阶段继续实施。

## P0-1 回归结果

本次结构改造前后：

- Recall@5：`1.0 -> 1.0`
- MRR：`1.0 -> 1.0`
- 答案覆盖率：`1.0 -> 1.0`
- 引用准确率：`0.2 -> 0.2`
- 索引峰值内存：增加约 `56 KiB`
- 索引磁盘：增加约 `8.8 KiB`

结构元数据没有改变当前检索与回答质量，磁盘增量来自节点 ID、哈希、解析器版本和引用锚点字段。
