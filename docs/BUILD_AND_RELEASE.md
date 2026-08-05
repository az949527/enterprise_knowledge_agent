# Build And Release

所有构建均要求 Python 3.11。PyInstaller 产物与操作系统相关，Windows 和 macOS 必须分别构建。

## Windows 桌面版

准备桌面环境：

```powershell
.\scripts\install_desktop.ps1
```

构建：

```powershell
.\scripts\build_desktop_windows.ps1
```

产物：

```text
outputs/releases/windows/LocalKnowledgeTool/LocalKnowledgeTool.exe
outputs/releases/LocalKnowledgeTool-Windows-x64.zip
```

脚本会调用 `scripts/verify_desktop_release.py`，阻止用户索引、文档名、`.env` 或 API Key 进入发布包。

## macOS 桌面版

macOS `.app` 必须在 macOS 上构建。要求 macOS 12 或更高版本，并可使用 Python 3.11。

```bash
chmod +x scripts/build_desktop_macos.sh
./scripts/build_desktop_macos.sh
```

产物：

```text
outputs/releases/macos/LocalKnowledgeTool.app
outputs/releases/LocalKnowledgeTool-macOS.zip
```

应用当前未签名。首次打开可能需要右键选择 **Open**；公开分发需要 Apple Developer ID 签名和 notarization。

## GitHub Actions macOS 构建

`.github/workflows/build-desktop-macos.yml` 构建：

- Apple Silicon `arm64`
- Intel `x86_64`

手动运行：

1. 推送代码到 GitHub。
2. 打开 **Actions**。
3. 选择 **Build macOS Desktop**。
4. 点击 **Run workflow**。

`desktop-v*` 标签推送会自动触发构建（普通代码推送不再触发）：

```bash
git tag desktop-v1.0.0
git push origin desktop-v1.0.0
```

每个 artifact 包含架构对应的 ZIP 和 SHA-256 文件，保留 14 天。

## Windows 轻量版

轻量版是单文件本地 Web 工具：

```powershell
.\scripts\install.ps1
.\scripts\build_lite_windows.ps1
```

产物：

```text
dist/LocalKnowledgeTool.exe
```

## macOS 轻量版

```bash
chmod +x scripts/build_lite_macos.sh
./scripts/build_lite_macos.sh
```

产物：

```text
dist/LocalKnowledgeTool
```

## 发布边界

所有桌面发布包必须从空用户状态启动：

- 不包含上传文档。
- 不包含 `nodes.jsonl`、`parents.jsonl`、`chunks.jsonl`、BM25、Embedding、FAISS 或 SQLite 数据。
- 不包含 `.env`。
- 不包含开发机保存的 LLM、Embedding 或 Reranker API Key。

正式发布前还应按 `DEVELOPMENT_PLAN.md` 记录 ZIP/目录体积、启动内存、索引峰值内存、查询峰值内存和索引磁盘占用。
