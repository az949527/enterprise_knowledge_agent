param(
    [string]$IndexUrl = "https://pypi.tuna.tsinghua.edu.cn/simple"
)

$ErrorActionPreference = "Stop"

$Root = (Split-Path -Parent $PSScriptRoot)
Set-Location $Root

$VenvPython = Join-Path $Root ".venv-desktop\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $VenvPython)) {
    throw "Desktop environment is missing. Run .\scripts\install_desktop.ps1 first."
}
$Version = & $VenvPython -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"
if ($Version.Trim() -ne "3.11") {
    throw "Desktop environment must use Python 3.11. Current version: $Version"
}

Write-Host "Installing build dependency..."
$Deps = & $VenvPython scripts/requirements_sections.py build
& $VenvPython -m pip install `
  --progress-bar on `
  --index-url $IndexUrl `
  @Deps

$ReleaseRoot = Join-Path $Root "outputs\releases"
$WindowsDist = Join-Path $ReleaseRoot "windows"
$BuildRoot = Join-Path $Root "outputs\build\windows"
$SpecRoot = Join-Path $Root "outputs\build\spec"
$ZipPath = Join-Path $ReleaseRoot "LocalKnowledgeTool-Windows-x64.zip"

foreach ($Path in @($WindowsDist, $BuildRoot, $SpecRoot)) {
    $ResolvedParent = [System.IO.Path]::GetFullPath((Split-Path -Parent $Path))
    if (-not $ResolvedParent.StartsWith([System.IO.Path]::GetFullPath($Root), [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to clean path outside project: $Path"
    }
    if (Test-Path -LiteralPath $Path) {
        Remove-Item -LiteralPath $Path -Recurse -Force
    }
}
if (Test-Path -LiteralPath $ZipPath) {
    Remove-Item -LiteralPath $ZipPath -Force
}

New-Item -ItemType Directory -Force -Path $ReleaseRoot, $WindowsDist, $BuildRoot, $SpecRoot | Out-Null

Write-Host "Building Windows desktop release..."
& $VenvPython -m PyInstaller `
  --noconfirm `
  --clean `
  --windowed `
  --onedir `
  --name LocalKnowledgeTool `
  --distpath $WindowsDist `
  --workpath $BuildRoot `
  --specpath $SpecRoot `
  --paths $Root `
  --hidden-import aiosqlite `
  --hidden-import sqlalchemy.dialects.sqlite.aiosqlite `
  scripts\run_desktop.py

$AppDir = Join-Path $WindowsDist "LocalKnowledgeTool"
& $VenvPython scripts\verify_desktop_release.py $AppDir

Start-Sleep -Seconds 3
& tar.exe -a -c -f $ZipPath -C $WindowsDist "LocalKnowledgeTool"
if ($LASTEXITCODE -ne 0) {
    throw "Failed to create Windows release zip."
}
& $VenvPython scripts\verify_desktop_release.py $AppDir

Write-Host ""
Write-Host "Windows release ready:"
Write-Host "  $AppDir\LocalKnowledgeTool.exe"
Write-Host "  $ZipPath"
