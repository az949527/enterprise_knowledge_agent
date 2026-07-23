# macOS Desktop Build

The macOS application must be built on macOS. PyInstaller cannot create a macOS
`.app` from Windows.

## Requirements

- macOS 12 or newer
- Python 3.11
- Internet access for the first dependency installation

## Build

Open Terminal in the extracted build-kit directory:

```bash
chmod +x scripts/build_desktop_macos.sh
./scripts/build_desktop_macos.sh
```

The output files are:

```text
outputs/releases/macos/LocalKnowledgeTool.app
outputs/releases/LocalKnowledgeTool-macOS.zip
```

The generated application starts with:

- no uploaded documents
- no local index
- no LLM API Key

The application is unsigned. On first launch, macOS may require right-clicking
the application and choosing **Open**. Public distribution requires Apple code
signing and notarization.
