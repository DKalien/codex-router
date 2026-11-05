$ErrorActionPreference = "Stop"
$Root = $PSScriptRoot
$Target = if ($env:MODEL_ROUTER_TARGET) { $env:MODEL_ROUTER_TARGET } else { "codex" }
if ($Target -ne "codex") {
  throw "MODEL_ROUTER_TARGET must be codex."
}
$Command = if ($args.Count) { [string]$args[0] } else { "" }
$Arguments = @($args | Select-Object -Skip 1)
$Commands = @(
  "install", "provider-key", "enable", "disable", "uninstall", "start", "status", "route", "pi", "monitor"
)
if ($Command -notin $Commands) {
  throw "Unknown command '$Command'. Choose: $($Commands -join ', ')."
}
function Invoke-RouterNode([string]$Script, [string[]]$ScriptArguments = @()) {
  & node (Join-Path $Root $Script) @ScriptArguments
  if ($LASTEXITCODE -ne 0) {
    throw "$Script exited with status $LASTEXITCODE."
  }
}

switch ($Command) {
  "route" { Invoke-RouterNode "src\gpt-route.mjs" $Arguments }
  "pi" { Invoke-RouterNode "src\pi-config.mjs" $Arguments }
  "provider-key" { Invoke-RouterNode "src\provider-key.mjs" $Arguments }
  "install" { & (Join-Path $Root "install.ps1") -Target $Target @Arguments }
  "enable" { & (Join-Path $Root "install.ps1") -Target $Target }
  "disable" {
    Invoke-RouterNode "src\config-manager.mjs" @("disable")
    Invoke-RouterNode "src\service.mjs" @("uninstall")
  }
  "uninstall" {
    Invoke-RouterNode "src\config-manager.mjs" @("disable")
    Invoke-RouterNode "src\service.mjs" @("uninstall")
  }
  "start" { Invoke-RouterNode "src\start.mjs" $Arguments }
  "status" { Invoke-RouterNode "src\status.mjs" $Arguments }
  "monitor" {
    $Launcher = Join-Path $Root "mimo-token-monitor\launcher.py"
    if (-not (Test-Path -LiteralPath $Launcher)) { throw "找不到合并的 MiMo Token Monitor：$Launcher" }
    $Python = Get-Command pythonw.exe -ErrorAction SilentlyContinue
    if (-not $Python) { $Python = Get-Command python.exe -ErrorAction SilentlyContinue }
    if (-not $Python) { throw "运行 MiMo Token Monitor 需要 Python 3；请先安装 Python 并安装 mimo-token-monitor\requirements.txt。" }
    & $Python.Source $Launcher
    if ($LASTEXITCODE -ne 0) { throw "MiMo Token Monitor exited with status $LASTEXITCODE." }
  }
}

exit 0
