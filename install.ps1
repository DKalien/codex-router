[CmdletBinding()]
param(
  [switch]$PrepareOnly,
  [switch]$ForceDeps,
  [ValidateSet("codex")]
  [string]$Target = "codex"
)

$ErrorActionPreference = "Stop"
$env:MODEL_ROUTER_TARGET = $Target

# Lite: this installer only runs from a local checkout. Clone/update is
# managed with git by the operator, not by the installer.

function Assert-Command([string]$Name, [string]$Help) {
  if (-not (Get-Command $Name -ErrorAction SilentlyContinue)) {
    throw "$Name is required. $Help"
  }
}

function Test-RouterCheckout([string]$Directory) {
  $Package = Join-Path $Directory "package.json"
  if (-not (Test-Path $Package)) { return $false }
  try {
    return (Get-Content $Package -Raw | ConvertFrom-Json).name -eq "codex-model-router"
  } catch {
    return $false
  }
}

$ScriptDirectory = $PSScriptRoot
if (-not $ScriptDirectory) { $ScriptDirectory = (Get-Location).Path }

if (-not (Test-RouterCheckout $ScriptDirectory)) {
  throw "install.ps1 must be run from a Codex Router checkout."
}

Assert-Command "node" "Install Node.js 24 LTS from https://nodejs.org/."
Assert-Command "npm" "npm is included with Node.js."
$VersionParts = (node -p "process.versions.node").Split(".")
if ([int]$VersionParts[0] -lt 22 -or
    ([int]$VersionParts[0] -eq 22 -and [int]$VersionParts[1] -lt 19)) {
  throw "Node.js 22.19 or newer is required; Node.js 24 LTS is recommended."
}

Push-Location $ScriptDirectory
try {
  $CodexHome = if ($env:CODEX_HOME) { $env:CODEX_HOME } else { Join-Path $HOME ".codex" }
  New-Item -ItemType Directory -Force -Path $CodexHome | Out-Null
  & node src/legacy-migration.mjs assert-clear | Out-Null
  if ($LASTEXITCODE -ne 0) { throw "Resolve the detected older router before installing." }
  if (-not $PrepareOnly) {
    & node src/provider-selection.mjs ensure-configured | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "Configure at least one provider before installing." }
  }

  # Lite: dependency steps are skipped when their output directories already
  # exist; -ForceDeps rebuilds them.
  $NodeModules = Join-Path $ScriptDirectory "node_modules"
  if (-not $ForceDeps -and (Test-Path $NodeModules)) {
    Write-Host "Node dependencies present; skipping npm ci. Use -ForceDeps to rebuild."
  } else {
    & npm ci --omit=dev
    if ($LASTEXITCODE -ne 0) { throw "npm dependency installation failed." }
  }

  & node src/secret.mjs ensure
  if ($LASTEXITCODE -ne 0) { throw "Local router-key setup failed." }
  $StateRoot = if ($env:MODEL_ROUTER_STATE_DIR) { $env:MODEL_ROUTER_STATE_DIR }
    elseif ($env:CODEX_ROUTER_STATE_DIR) { $env:CODEX_ROUTER_STATE_DIR }
    elseif ($env:CODEX_HOME) { Join-Path $env:CODEX_HOME "codex-router" }
    else { Join-Path $HOME ".codex\codex-router" }
  if (Test-Path (Join-Path $StateRoot "native-models.json")) {
    & node src/catalog.mjs
  } else {
    & node src/catalog.mjs --refresh-native
  }
  if ($LASTEXITCODE -ne 0) { throw "Codex model-catalog generation failed." }

  if ($PrepareOnly) {
    Write-Host "Dependencies and generated files are prepared; application configuration was not changed."
    exit 0
  }

  $ConfigManager = "src\config-manager.mjs"
  $ConfigEnabled = $false
  $ServiceInstalled = $false
  try {
    $ConfigEnabled = $true
    & node $ConfigManager enable
    if ($LASTEXITCODE -ne 0) { throw "$Target configuration update failed." }
    $ServiceInstalled = $true
    & node src/service.mjs install
    if ($LASTEXITCODE -ne 0) { throw "Background-service installation failed." }
    & node src/wait-health.mjs
    if ($LASTEXITCODE -ne 0) { throw "The router did not become healthy." }
    & node src/install-manifest.mjs record | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "Install-manifest recording failed." }
  } catch {
    if ($ServiceInstalled) { & node src/service.mjs uninstall 2>$null | Out-Null }
    if ($ConfigEnabled) { & node $ConfigManager disable 2>$null | Out-Null }
    throw
  }
  Write-Host "Installed the selected external model routes. Fully quit and reopen Codex."
  Write-Host "MiMo API key: .\codex-router.ps1 provider-key mimo-token-plan set"
  Write-Host "WLB API key:  .\codex-router.ps1 provider-key wlb-relay set"
} finally {
  Pop-Location
}
