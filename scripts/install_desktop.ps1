param(
    [string]$IndexUrl = "https://pypi.tuna.tsinghua.edu.cn/simple",
    [string]$PythonExe = "$env:LOCALAPPDATA\Programs\Python\Python311\python.exe"
)

$ErrorActionPreference = "Stop"

Set-Location (Split-Path -Parent $PSScriptRoot)

if (-not (Test-Path -LiteralPath $PythonExe)) {
    $PythonExe = "python"
}

$VenvPython = Join-Path (Get-Location) ".venv-desktop\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $VenvPython)) {
    Write-Host "Creating desktop virtual environment..."
    & $PythonExe -m venv .venv-desktop
}

Write-Host "Installing desktop dependencies..."
Write-Host "Qt Essentials download is approximately 75 MB."
Write-Host "Package index: $IndexUrl"
Write-Host ""

& $VenvPython -m pip install `
  --progress-bar on `
  --timeout 120 `
  --retries 5 `
  --index-url $IndexUrl `
  -r requirements-desktop.txt

Write-Host ""
Write-Host "Desktop dependencies installed."
Write-Host "Run the application with:"
Write-Host "  .\scripts\run_desktop.ps1"
