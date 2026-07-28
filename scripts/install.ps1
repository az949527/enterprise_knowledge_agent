param(
    [string]$IndexUrl = "https://pypi.tuna.tsinghua.edu.cn/simple",
    [string]$PythonExe = "$env:LOCALAPPDATA\Programs\Python\Python311\python.exe"
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

if (-not (Test-Path -LiteralPath $PythonExe)) {
    throw "Python 3.11 was not found at: $PythonExe"
}

$Version = & $PythonExe -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"
if ($Version.Trim() -ne "3.11") {
    throw "Python 3.11 is required. Selected interpreter reports: $Version"
}

$VenvPython = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $VenvPython)) {
    Write-Host "Creating Python 3.11 backend environment..."
    & $PythonExe -m venv .venv
}

Write-Host "Installing backend dependencies..."
& $VenvPython -m pip install `
  --progress-bar on `
  --timeout 120 `
  --retries 5 `
  --index-url $IndexUrl `
  -r requirements.txt

Write-Host ""
Write-Host "Backend environment ready:"
Write-Host "  .\.venv\Scripts\python.exe"
Write-Host "Run the backend with:"
Write-Host "  .\scripts\run_web.ps1"
