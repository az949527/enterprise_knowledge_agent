#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

PYTHON_BIN="${PYTHON_BIN:-python3.11}"
PYTHON_VERSION="$("$PYTHON_BIN" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
if [[ "$PYTHON_VERSION" != "3.11" ]]; then
  echo "Python 3.11 is required. Current version: $PYTHON_VERSION" >&2
  exit 1
fi

DEPS="$("$PYTHON_BIN" scripts/requirements_sections.py server build)"
"$PYTHON_BIN" -m pip install $DEPS

"$PYTHON_BIN" -m PyInstaller \
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
