param(
    [string]$IndexUrl = "https://pypi.tuna.tsinghua.edu.cn/simple",
    [string]$PythonExe = "$env:LOCALAPPDATA\Programs\Python\Python311\python.exe"
)

$ErrorActionPreference = "Stop"

Set-Location (Split-Path -Parent $PSScriptRoot)

if (-not (Test-Path -LiteralPath $PythonExe)) {
    throw "Python 3.11 was not found at: $PythonExe"
}

$Version = & $PythonExe -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"
if ($Version.Trim() -ne "3.11") {
    throw "Python 3.11 is required. Selected interpreter reports: $Version"
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

$Deps = & $VenvPython scripts/requirements_sections.py desktop
& $VenvPython -m pip install `
  --progress-bar on `
  --timeout 120 `
  --retries 5 `
  --index-url $IndexUrl `
  @Deps

Write-Host ""
Write-Host "Desktop dependencies installed."
Write-Host "Run the application with:"
Write-Host "  .\scripts\run_desktop.ps1"
