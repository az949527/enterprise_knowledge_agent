#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

PYTHON_BIN="${PYTHON_BIN:-python3.11}"
INDEX_URL="${INDEX_URL:-https://pypi.org/simple}"
VENV_DIR="$ROOT/.venv-desktop-macos"

if [[ ! -x "$VENV_DIR/bin/python" ]]; then
  "$PYTHON_BIN" -m venv "$VENV_DIR"
fi

"$VENV_DIR/bin/python" -m pip install \
  --progress-bar on \
  --index-url "$INDEX_URL" \
  -r requirements-desktop.txt \
  -r requirements-desktop-build.txt

RELEASE_ROOT="$ROOT/outputs/releases"
DIST_DIR="$RELEASE_ROOT/macos"
BUILD_DIR="$ROOT/outputs/build/macos"
SPEC_DIR="$ROOT/outputs/build/spec-macos"
ZIP_PATH="$RELEASE_ROOT/LocalKnowledgeTool-macOS.zip"

rm -rf "$DIST_DIR" "$BUILD_DIR" "$SPEC_DIR"
rm -f "$ZIP_PATH"
mkdir -p "$DIST_DIR" "$BUILD_DIR" "$SPEC_DIR" "$RELEASE_ROOT"

"$VENV_DIR/bin/python" -m PyInstaller \
  --noconfirm \
  --clean \
  --windowed \
  --onedir \
  --name LocalKnowledgeTool \
  --distpath "$DIST_DIR" \
  --workpath "$BUILD_DIR" \
  --specpath "$SPEC_DIR" \
  --paths "$ROOT" \
  scripts/run_desktop.py

"$VENV_DIR/bin/python" scripts/verify_desktop_release.py "$DIST_DIR/LocalKnowledgeTool.app"
ditto -c -k --sequesterRsrc --keepParent "$DIST_DIR/LocalKnowledgeTool.app" "$ZIP_PATH"

echo
echo "macOS release ready:"
echo "  $DIST_DIR/LocalKnowledgeTool.app"
echo "  $ZIP_PATH"
echo
echo "The app is unsigned. For public distribution, sign and notarize it with an Apple Developer ID."
