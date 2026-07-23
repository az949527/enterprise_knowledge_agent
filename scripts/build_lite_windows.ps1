$ErrorActionPreference = "Stop"

Set-Location (Split-Path -Parent $PSScriptRoot)

python -m pip install -r requirements-lite.txt

python -m PyInstaller `
  --name LocalKnowledgeTool `
  --onefile `
  --clean `
  --add-data "app/lite/static;app/lite/static" `
  --add-data "app;app" `
  --hidden-import "uvicorn.logging" `
  --hidden-import "uvicorn.loops" `
  --hidden-import "uvicorn.loops.auto" `
  --hidden-import "uvicorn.protocols" `
  --hidden-import "uvicorn.protocols.http" `
  --hidden-import "uvicorn.protocols.http.auto" `
  --hidden-import "uvicorn.protocols.websockets" `
  --hidden-import "uvicorn.protocols.websockets.auto" `
  --hidden-import "uvicorn.lifespan" `
  --hidden-import "uvicorn.lifespan.on" `
  scripts/run_lite_portable.py

Write-Host ""
Write-Host "Build finished:"
Write-Host "  dist/LocalKnowledgeTool.exe"
Write-Host ""
Write-Host "Send dist/LocalKnowledgeTool.exe to Windows users."
