#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

python3 -m pip install -r requirements-lite.txt

python3 -m PyInstaller \
  --name LocalKnowledgeTool \
  --onefile \
  --clean \
  --add-data "app/lite/static:app/lite/static" \
  --add-data "app:app" \
  --hidden-import "uvicorn.logging" \
  --hidden-import "uvicorn.loops" \
  --hidden-import "uvicorn.loops.auto" \
  --hidden-import "uvicorn.protocols" \
  --hidden-import "uvicorn.protocols.http" \
  --hidden-import "uvicorn.protocols.http.auto" \
  --hidden-import "uvicorn.protocols.websockets" \
  --hidden-import "uvicorn.protocols.websockets.auto" \
  --hidden-import "uvicorn.lifespan" \
  --hidden-import "uvicorn.lifespan.on" \
  scripts/run_lite_portable.py

echo
echo "Build finished:"
echo "  dist/LocalKnowledgeTool"
echo
echo "Send dist/LocalKnowledgeTool to macOS users."
