$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

$VenvPython = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $VenvPython)) {
    throw "Backend environment is missing. Run .\scripts\install.ps1 first."
}

& $VenvPython -c "import app"
& $VenvPython -m uvicorn app.main:app --reload
