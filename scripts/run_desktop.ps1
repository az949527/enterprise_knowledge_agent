$ErrorActionPreference = "Stop"

Set-Location (Split-Path -Parent $PSScriptRoot)

$VenvPython = Join-Path (Get-Location) ".venv-desktop\Scripts\python.exe"
$VenvPythonw = Join-Path (Get-Location) ".venv-desktop\Scripts\pythonw.exe"
if (-not (Test-Path -LiteralPath $VenvPython)) {
    throw "Desktop environment is missing. Run .\scripts\install_desktop.ps1 first."
}

if ($args -contains "--console") {
    & $VenvPython scripts\run_desktop.py @args
    exit $LASTEXITCODE
}

Start-Process `
  -FilePath $VenvPythonw `
  -ArgumentList @("scripts\run_desktop.py") `
  -WorkingDirectory (Get-Location)
