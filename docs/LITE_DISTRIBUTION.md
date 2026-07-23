# Lightweight Distribution

This document is for packaging the lite local knowledge tool for users who do not
have Python installed.

## Recommended Delivery

Build one executable per operating system:

- Windows users receive `LocalKnowledgeTool.exe`.
- macOS users receive `LocalKnowledgeTool`.

The executable starts a local web server, opens the browser automatically, and
stores uploaded-file indexes beside the executable under `data/lite_index`.

## Build On Windows

Run from the project root:

```powershell
.\scripts\build_lite_windows.ps1
```

Send this file to Windows users:

```text
dist\LocalKnowledgeTool.exe
```

## Build On macOS

PyInstaller builds are OS-specific. To create a macOS executable, run this on a
Mac:

```bash
chmod +x scripts/build_lite_macos.sh
./scripts/build_lite_macos.sh
```

Send this file to macOS users:

```text
dist/LocalKnowledgeTool
```

## End User Instructions

1. Double-click `LocalKnowledgeTool`.
2. Wait for the browser to open.
3. Select files or a folder.
4. Ask questions after indexing finishes.
5. Close the terminal window to stop the tool.

If the browser does not open automatically, use the URL printed in the terminal,
usually:

```text
http://127.0.0.1:8010/
```

## LLM Key

If LLM answers are needed, configure `LLM_API_KEY`, `LLM_BASE_URL`, and
`LLM_MODEL` in the environment before launching, or replace the default settings
before building. Do not distribute a private API key in a public executable.
