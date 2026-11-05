[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
$projectRoot = $PSScriptRoot
$buildRoot = Join-Path $projectRoot '.build-launcher'
$specPath = Join-Path $buildRoot 'spec'
$workPath = Join-Path $buildRoot 'work'
$distPath = Join-Path $projectRoot 'dist'
$launcherPath = Join-Path $projectRoot 'launcher.py'

if (-not (Test-Path -LiteralPath $launcherPath -PathType Leaf)) {
    throw "Launcher source was not found: $launcherPath"
}

New-Item -ItemType Directory -Force -Path $specPath, $workPath, $distPath | Out-Null

# Keep launcher-generated specs and intermediate files outside the project root so
# the independent full-release MiMo-Token-Monitor.spec is never overwritten.
& python -m PyInstaller `
    --noconfirm `
    --clean `
    --onefile `
    --windowed `
    --name 'MiMo-Token-Monitor' `
    --icon (Join-Path $projectRoot 'icon.ico') `
    --specpath $specPath `
    --workpath $workPath `
    --distpath $distPath `
    $launcherPath

if ($LASTEXITCODE -ne 0) {
    throw "PyInstaller failed with exit code $LASTEXITCODE."
}

Write-Host "Lightweight launcher created: $(Join-Path $distPath 'MiMo-Token-Monitor.exe')"
