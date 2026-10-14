import { execFileSync } from "node:child_process";
import {
  existsSync,
  mkdirSync,
  readFileSync,
  renameSync,
  unlinkSync,
  writeFileSync,
} from "node:fs";
import path from "node:path";

import {
  CODEX_HOME,
  LOG_PATH,
  PORTS,
  SHUTDOWN_REQUEST_PATH,
  SOURCE_ROOT,
  STATE_DIR,
  TARGET,
} from "./paths.mjs";
import { SHUTDOWN_DRAIN_MS, SHUTDOWN_FLUSH_MS } from "./http-utils.mjs";
import { waitForRouterHealth } from "./router-health.mjs";

const effectivePlatform = process.env.CODEX_ROUTER_SERVICE_PLATFORM || process.platform;
const command = process.argv[2] || "status";
const renderCommands = new Set(["render", "render-launcher", "render-task"]);
const taskName = process.env.CODEX_ROUTER_TASK_NAME || "Codex Router";
const wrapperPath = path.join(STATE_DIR, "start-codex-router.cmd");
const launcherPath = path.join(STATE_DIR, "start-codex-router-hidden.vbs");
const servicePidPath = path.join(STATE_DIR, "service.pid");
const startPath = path.join(SOURCE_ROOT, "src", "start.mjs");

if (effectivePlatform !== "win32" && !renderCommands.has(command)) {
  throw new Error("The Task Scheduler service manager runs on Windows only.");
}

function cmdEscape(value) {
  return String(value).replaceAll("%", "%%").replaceAll('"', '""');
}

function vbsEscape(value) {
  return String(value).replaceAll('"', '""');
}

function serviceEnvironment() {
  return {
    MODEL_ROUTER_TARGET: TARGET,
    MODEL_ROUTER_STATE_DIR: STATE_DIR,
    MODEL_ROUTER_QUIET: "1",
    MODEL_ROUTER_PORT: String(PORTS.router),
    CODEX_HOME,
    CODEX_ROUTER_STATE_DIR: STATE_DIR,
    CODEX_ROUTER_QUIET: "1",
    CODEX_ROUTER_PORT: String(PORTS.router),
    CODEX_ROUTER_SERVICE_PID_PATH: servicePidPath,
    ...(process.env.KIMI_CODE_HOME ? { KIMI_CODE_HOME: process.env.KIMI_CODE_HOME } : {}),
  };
}

function wrapper() {
  return `@echo off\r\n${Object.entries(serviceEnvironment())
    .map(([key, value]) => `set "${key}=${cmdEscape(value)}"`)
    .join("\r\n")}\r\n"${cmdEscape(process.execPath)}" "${cmdEscape(startPath)}" >> "${cmdEscape(LOG_PATH)}" 2>&1\r\n`;
}

// The scheduled task launches this script through `wscript.exe //B //NoLogo`,
// which is a windowless host, and the script starts the CMD wrapper with a
// window style of 0. Without it the wrapper owned a console window that stayed
// on screen for the router's lifetime and reappeared on every watchdog restart.
//
// The `True` wait flag is what keeps Task Scheduler's restart settings alive:
// Run then blocks until the wrapper exits and returns its exit code, which the
// script re-raises through WScript.Quit. Quitting with a fixed 0 (or letting the
// script fall off the end) would report every crash as a clean exit and silently
// disable RestartCount/RestartInterval.
function launcher() {
  // A Windows path cannot contain a double quote, but escape it anyway so a
  // hand-edited state directory can never break out of the string literal.
  // Chr(34) supplies the quotes cmd.exe needs around the wrapper path, which
  // keeps this generated source free of stacked quote-doubling.
  return [
    "Option Explicit",
    "",
    "Dim quote, shell, env, status",
    "quote = Chr(34)",
    'Set shell = CreateObject("WScript.Shell")',
    'Set env = shell.Environment("PROCESS")',
    ...Object.entries(serviceEnvironment()).map(
      ([key, value]) => `env("${key}") = "${vbsEscape(value)}"`,
    ),
    "On Error Resume Next",
    // The scheduled task runs node.exe directly instead of the .cmd wrapper:
    // cmd.exe parses its command line as Unicode, but parses batch FILES in
    // the system ANSI code page, and a non-ASCII install location made the
    // .cmd fail with a "path not found" error.
    `status = shell.Run("cmd.exe /D /C " & quote & quote & "${vbsEscape(process.execPath)}" & quote & " " & quote & "${vbsEscape(startPath)}" & quote & " >> " & quote & "${vbsEscape(LOG_PATH)}" & quote & " 2>&1" & quote, 0, True)`,
    "If Err.Number <> 0 Then",
    "  WScript.Quit 1",
    "End If",
    "On Error Goto 0",
    "WScript.Quit status",
    "",
  ].join("\r\n");
}

function schtasks(args, options = {}) {
  return execFileSync("schtasks.exe", args, {
    encoding: "utf8",
    stdio: options.quiet ? ["ignore", "ignore", "ignore"] : ["ignore", "pipe", "pipe"],
    windowsHide: true,
    timeout: TASK_STATE_TIMEOUT_MS,
  });
}

function writeAtomic(target, contents) {
  const temporary = `${target}.tmp.${process.pid}`;
  writeFileSync(temporary, contents);
  // renameSync replaces an existing destination on Windows, so reinstalling
  // over an older launcher pair is a plain overwrite rather than a conflict.
  renameSync(temporary, target);
}

function writeLaunchers() {
  mkdirSync(STATE_DIR, { recursive: true });
  writeAtomic(wrapperPath, Buffer.from(wrapper(), "utf8"));
  // wscript.exe parses a script file with the system ANSI code page unless the
  // file carries a UTF-16 byte order mark, so a state directory holding
  // non-ASCII characters only round-trips when the launcher is UTF-16LE.
  writeAtomic(
    launcherPath,
    Buffer.concat([Buffer.from([0xff, 0xfe]), Buffer.from(launcher(), "utf16le")]),
  );
}

// `//B` suppresses script errors and prompts, `//NoLogo` suppresses the banner;
// neither host allocates a console, so nothing is drawn at logon.
function taskAction() {
  return {
    execute: "wscript.exe",
    // Unlike cmd.exe, wscript.exe follows the standard command-line parser, so
    // the launcher path takes a single quote pair. cmd.exe's doubled-quote form
    // would parse as an empty argument followed by a split path.
    argument: `//B //NoLogo "${launcherPath}"`,
  };
}

function installTask() {
  const { execute, argument } = taskAction();
  const script = [
    // The action strings travel through the environment so that the quotes
    // around the launcher path never pass through powershell.exe's -Command
    // reparse or the schtasks argument escaper.
    "$action = New-ScheduledTaskAction -Execute $env:CODEX_ROUTER_TASK_EXECUTE -Argument $env:CODEX_ROUTER_TASK_ARGUMENT",
    "$trigger = New-ScheduledTaskTrigger -AtLogOn -User ([Security.Principal.WindowsIdentity]::GetCurrent().Name)",
    "$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -MultipleInstances IgnoreNew",
    "$principal = New-ScheduledTaskPrincipal -UserId ([Security.Principal.WindowsIdentity]::GetCurrent().Name) -LogonType Interactive -RunLevel Limited",
    "Register-ScheduledTask -TaskName $env:CODEX_ROUTER_TASK -Action $action -Trigger $trigger -Settings $settings -Principal $principal -Force | Out-Null",
  ].join("; ");
  try {
    execFileSync(
      "powershell.exe",
      ["-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script],
      {
        env: {
          ...process.env,
          CODEX_ROUTER_TASK: taskName,
          CODEX_ROUTER_TASK_EXECUTE: execute,
          CODEX_ROUTER_TASK_ARGUMENT: argument,
        },
        windowsHide: true,
        timeout: TASK_STATE_TIMEOUT_MS,
        stdio: ["ignore", "ignore", "ignore"],
      },
    );
  } catch {
    schtasks(
      [
        "/Create",
        "/TN",
        taskName,
        "/SC",
        "ONLOGON",
        "/TR",
        `${execute} ${argument}`,
        "/RL",
        "LIMITED",
        "/F",
      ],
      { quiet: true },
    );
  }
}

// `schtasks /End` returns once Task Scheduler has accepted the request, not
// once the instance is gone, and `MultipleInstances IgnoreNew` silently drops a
// `/Run` issued while the old one is still winding down -- which leaves the
// router stopped until the next logon, and turns the installer's readiness wait
// into a five-minute stall followed by a rollback. Polling the real state beats
// retrying `/Run`: it continues as soon as the instance has actually gone
// instead of guessing how long that takes, and it gives up on a fixed deadline
// instead of hoping one extra attempt is enough.
const TASK_STOP_TIMEOUT_MS = 10_000;
const TASK_STOP_POLL_MS = 250;
// 优雅停止的等待窗口：路由器的排空（drain）+ 收尾（flush）+ 进程退出余量。
// 超时后走原有的强杀路径，停止命令不会无限等待。
const GRACEFUL_STOP_TIMEOUT_MS = SHUTDOWN_DRAIN_MS + SHUTDOWN_FLUSH_MS + 3_000;
// /Run 之后等待路由器就绪的上限：start.mjs 给路由器 30 秒启动预算，这里再覆盖
// wscript -> cmd -> node 的拉起开销。
const READINESS_TIMEOUT_MS = 60_000;
// Every state query has to return for the deadline above to mean anything, so a
// wedged PowerShell is capped rather than allowed to hang the install outright.
const TASK_STATE_TIMEOUT_MS = 15_000;

function sleep(milliseconds) {
  Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, milliseconds);
}

function waitForTaskToStop() {
  const deadline = Date.now() + TASK_STOP_TIMEOUT_MS;
  while (Date.now() < deadline) {
    const state = taskState(deadline);
    if (Date.now() >= deadline) break;
    if (state === undefined || state === "unknown") {
      throw new Error("Unable to verify the scheduled task state; refusing to continue.");
    }
    if (state !== "running" && state !== "queued") return;
    sleep(Math.min(TASK_STOP_POLL_MS, deadline - Date.now()));
  }
  throw new Error("Timed out waiting for the Codex Router scheduled task to stop.");
}

function readServicePid() {
  try {
    const value = readFileSync(servicePidPath, "utf8").trim();
    if (!/^\d+$/.test(value)) return undefined;
    const pid = Number(value);
    return Number.isSafeInteger(pid) && pid > 0 ? pid : undefined;
  } catch {
    return undefined;
  }
}

function removeServicePid(pid) {
  try {
    if (readFileSync(servicePidPath, "utf8").trim() === String(pid)) {
      unlinkSync(servicePidPath);
    }
  } catch {
    // The process may already have removed its PID file while exiting.
  }
}

function serviceNodePath() {
  let launcher;
  try {
    const bytes = readFileSync(launcherPath);
    launcher = bytes.subarray(0, 2).equals(Buffer.from([0xff, 0xfe]))
      ? bytes.subarray(2).toString("utf16le")
      : bytes.toString("utf8");
  } catch (error) {
    if (error.code !== "ENOENT") throw error;
  }
  const direct = launcher?.match(/^\s*status = shell\.Run\("cmd\.exe \/D \/C " & quote & quote & "((?:""|[^"])*)" & quote & " " & quote & "((?:""|[^"])*)" & quote & " >> "/m);
  if (direct && path.resolve(direct[2].replaceAll('""', '"')).toLowerCase() === startPath.toLowerCase()) {
    return direct[1].replaceAll('""', '"');
  }
  // Older VBS launchers delegate to the registered CMD wrapper.
  if (launcher && !launcher.includes(`"${vbsEscape(wrapperPath)}"`)) {
    throw new Error("Unable to identify Node in the registered Codex Router launcher.");
  }
  try {
    const wrapper = readFileSync(wrapperPath, "utf8");
    const command = wrapper.match(/^"([^"]+)" "([^"]+)" >> /m);
    if (command && path.resolve(command[2].replaceAll("%%", "%")).toLowerCase() === startPath.toLowerCase()) {
      return command[1].replaceAll("%%", "%");
    }
    throw new Error("Unable to identify Node in the registered Codex Router wrapper.");
  } catch (error) {
    if (error.code !== "ENOENT" || launcher) throw error;
    return process.execPath;
  }
}

function findServiceProcess(pid) {
  const nodePath = serviceNodePath();
  const script = [
    "try {",
    "  $expectedExe = [IO.Path]::GetFullPath($env:CODEX_ROUTER_NODE_BIN)",
    "  $expectedStart = [IO.Path]::GetFullPath($env:CODEX_ROUTER_START_SCRIPT)",
    pid
      ? "  $processes = @(Get-CimInstance Win32_Process -Filter ('ProcessId = ' + $env:CODEX_ROUTER_SERVICE_PID) -ErrorAction Stop)"
      : "  $processes = @(Get-CimInstance Win32_Process -ErrorAction Stop)",
    "  $matches = @($processes | Where-Object {",
    "    try {",
    "      $actualExe = [IO.Path]::GetFullPath($_.ExecutablePath)",
    "      $command = (($_.CommandLine -replace '\\s+', ' ').Trim())",
    "      $expectedCommands = @((\'\"{0}\" \"{1}\"\' -f $actualExe, $expectedStart), (\'{0} \"{1}\"\' -f $actualExe, $expectedStart), (\'\"{0}\" {1}\' -f $actualExe, $expectedStart), (\'{0} {1}\' -f $actualExe, $expectedStart))",
    "      ($actualExe -ieq $expectedExe) -and ($expectedCommands -icontains $command)",
    "    } catch { $false }",
    "  })",
    ...(!pid
      ? [
          "  $matches = @($matches | Where-Object {",
          "    try {",
          "      $parent = Get-CimInstance Win32_Process -Filter ('ProcessId = ' + $_.ParentProcessId) -ErrorAction Stop",
          "      $scriptHost = Get-CimInstance Win32_Process -Filter ('ProcessId = ' + $parent.ParentProcessId) -ErrorAction Stop",
          "      ($parent.Name -ieq 'cmd.exe') -and ($scriptHost.Name -ieq 'wscript.exe') -and ($scriptHost.CommandLine.IndexOf(('\"' + $env:CODEX_ROUTER_LAUNCHER_PATH + '\"'), [StringComparison]::OrdinalIgnoreCase) -ge 0)",
          "    } catch { $false }",
          "  })",
        ]
      : []),
    "  if ($matches.Count -gt 1) { exit 6 }",
    "  if ($matches.Count -eq 1) { [Console]::Out.Write($matches[0].ProcessId) }",
    "} catch { exit 5 }",
  ].join("; ");
  let lastError;
  for (const executable of ["pwsh.exe", "powershell.exe"]) {
    try {
      const output = execFileSync(
        executable,
        ["-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script],
        {
          encoding: "utf8",
          env: {
            ...process.env,
            CODEX_ROUTER_NODE_BIN: nodePath,
            CODEX_ROUTER_START_SCRIPT: startPath,
            CODEX_ROUTER_LAUNCHER_PATH: launcherPath,
            ...(pid ? { CODEX_ROUTER_SERVICE_PID: String(pid) } : {}),
          },
          stdio: ["ignore", "pipe", "ignore"],
          timeout: TASK_STATE_TIMEOUT_MS,
          windowsHide: true,
        },
      ).trim();
      return /^\d+$/.test(output) ? Number(output) : undefined;
    } catch (error) {
      if (error?.status === 6) {
        throw new Error("Multiple Codex Router supervisors are running; stop them manually.");
      }
      lastError = error;
    }
  }
  throw new Error("Unable to verify the Codex Router service process; refusing to terminate it.", {
    cause: lastError,
  });
}

function serviceProcessPid({ cleanup = true } = {}) {
  const trackedPid = readServicePid();
  if (trackedPid) {
    const verifiedPid = findServiceProcess(trackedPid);
    if (verifiedPid) return verifiedPid;
    if (cleanup) removeServicePid(trackedPid);
  }
  // Older launchers did not set a PID path. The exact wscript -> cmd -> start.mjs
  // parent chain keeps this one-time fallback from selecting a foreground Node.
  return findServiceProcess();
}

function stopServiceProcess(pid) {
  if (!pid) return;
  try {
    execFileSync("taskkill.exe", ["/PID", String(pid), "/T", "/F"], {
      stdio: ["ignore", "ignore", "ignore"],
      windowsHide: true,
      timeout: TASK_STATE_TIMEOUT_MS,
    });
  } catch (error) {
    if (findServiceProcess(pid)) {
      throw new Error(`Unable to terminate Codex Router service process ${pid}.`, {
        cause: error,
      });
    }
  }
  removeServicePid(pid);
}

function isProcessAlive(pid) {
  try {
    process.kill(pid, 0);
    return true;
  } catch (error) {
    // EPERM 表示进程存在但无权限探测；其余情况视为已退出。
    return error?.code === "EPERM";
  }
}

function removeShutdownRequest() {
  try {
    unlinkSync(SHUTDOWN_REQUEST_PATH);
  } catch {
    // 路由器可能已自行清理。
  }
}

// Windows 无法跨进程投递 SIGTERM，taskkill 又是硬杀，会把在途请求直接掐断。
// 优雅停止改为写入停止请求文件，让路由器按既有排空流程结束响应后退出；
// 返回 true 表示进程已在窗口内退出，false 表示需要强杀兜底。
function requestGracefulStop(pid) {
  if (!pid) return false;
  try {
    writeAtomic(SHUTDOWN_REQUEST_PATH, Buffer.from("stop\n", "utf8"));
  } catch {
    return false;
  }
  const deadline = Date.now() + GRACEFUL_STOP_TIMEOUT_MS;
  while (Date.now() < deadline) {
    if (!isProcessAlive(pid)) {
      removeShutdownRequest();
      return true;
    }
    sleep(TASK_STOP_POLL_MS);
  }
  removeShutdownRequest();
  return false;
}

function endTask() {
  const pid = serviceProcessPid();
  if (requestGracefulStop(pid)) {
    removeServicePid(pid);
    // 监督进程退出后 wscript/cmd 可能还在收尾，等任务实例真正结束再返回，
    // 避免随后的 /Run 被 MultipleInstances IgnoreNew 静默丢弃。
    waitForTaskToStop();
    return;
  }
  try {
    schtasks(["/End", "/TN", taskName], { quiet: true });
  } catch {
    // A missing or already-idle task can still have an orphaned process tree.
  }
  stopServiceProcess(pid);
  waitForTaskToStop();
}

// Only a task that still exists can be started. `Register-ScheduledTask -Force`
// unregisters before it registers, so a failed registration leaves either the
// previous definition or nothing at all, and `/Run` against a name that is gone
// recovers nothing while reporting an error of its own.
function taskExists() {
  try {
    schtasks(["/Query", "/TN", taskName], { quiet: true });
    return true;
  } catch {
    return false;
  }
}

function taskState(deadline = Date.now() + TASK_STATE_TIMEOUT_MS) {
  const script = [
    "try {",
    "  $scheduler = New-Object -ComObject Schedule.Service",
    "  $scheduler.Connect()",
    "  [Console]::Out.Write($scheduler.GetFolder('\\').GetTask($env:CODEX_ROUTER_TASK).State)",
    "} catch {",
    "  if ($_.Exception.GetBaseException().HResult -in @(-2147024894, -2147024893)) { [Console]::Out.Write('missing') } else { exit 1 }",
    "}",
  ].join("; ");
  for (const executable of ["pwsh.exe", "powershell.exe"]) {
    const remaining = deadline - Date.now();
    if (remaining <= 0) return undefined;
    try {
      const output = execFileSync(
        executable,
        ["-NoLogo", "-NoProfile", "-NonInteractive", "-Command", script],
        {
          encoding: "utf8",
          env: { ...process.env, CODEX_ROUTER_TASK: taskName },
          stdio: ["ignore", "pipe", "ignore"],
          timeout: Math.min(TASK_STATE_TIMEOUT_MS, remaining),
          windowsHide: true,
        },
      ).trim().toLowerCase();
      return { 0: "unknown", 1: "disabled", 2: "queued", 3: "ready", 4: "running", missing: "missing" }[output];
    } catch {
      // PowerShell Core 不可用时，在同一 deadline 内尝试 Windows PowerShell。
    }
  }
  return undefined;
}

// Node 的 fetch 不使用 Windows 系统代理；在 start.mjs 之外发起 fetch 的进程必须
// 自己携带代理环境变量。健康探测只打本机回环，NO_PROXY 强制包含回环地址，
// 保证探测始终直连、不依赖代理存活。
function ensureFetchProxyEnvironment() {
  const proxyUrl =
    process.env.HTTPS_PROXY ||
    process.env.https_proxy ||
    "http://127.0.0.1:7897";
  process.env.NODE_USE_ENV_PROXY = "1";
  process.env.HTTP_PROXY = process.env.HTTP_PROXY || proxyUrl;
  process.env.HTTPS_PROXY = process.env.HTTPS_PROXY || proxyUrl;
  process.env.NO_PROXY = [
    "127.0.0.1,localhost",
    process.env.NO_PROXY || process.env.no_proxy || "",
  ]
    .filter(Boolean)
    .join(",");
}

if (
  !new Set([
    "install",
    "uninstall",
    "start",
    "stop",
    "restart",
    "status",
    "render",
    "render-launcher",
    "render-task",
  ]).has(command)
) {
  console.error(
    "Usage: service-windows.mjs install|uninstall|start|stop|restart|status|render|render-launcher|render-task",
  );
  process.exit(2);
}

if (command === "render") {
  process.stdout.write(wrapper());
} else if (command === "render-launcher") {
  process.stdout.write(launcher());
} else if (command === "render-task") {
  process.stdout.write(`${JSON.stringify(taskAction())}\n`);
} else if (command === "install") {
  // 停止失败时不能恢复 /Run，否则可能与仍在退出的旧实例重叠。
  endTask();
  try {
    // An upgrade from the console-visible task may still have that instance
    // running. Register-ScheduledTask -Force replaces the definition under the
    // same task name, so no duplicate is left behind, but it does not stop the
    // running instance, and MultipleInstances IgnoreNew would then drop the new
    // hidden run — the console window would survive until the next logon.
    // Stop using the previous launcher's Node path before replacing it.
    writeLaunchers();
    installTask();
    schtasks(["/Run", "/TN", taskName], { quiet: true });
  } catch {
    // Scheduled-task creation can be restricted in a non-elevated terminal. The
    // caller can retry -- but endTask() may already have stopped whatever was
    // running by this point, so simply returning would take a working router
    // down in exchange for nothing. Start whichever definition survived the
    // failed registration. When none did there is nothing to restore: no
    // snapshot was taken, and re-creating the old console-visible action would
    // reintroduce the very defect this launcher exists to fix.
    try {
      if (taskExists()) schtasks(["/Run", "/TN", taskName], { quiet: true });
    } catch {
      // Nothing left to start; the caller's readiness check reports the failure.
    }
  }
  process.stdout.write(`${JSON.stringify({ installed: true, path: wrapperPath })}\n`);
} else if (command === "uninstall") {
  endTask();
  try {
    schtasks(["/Delete", "/TN", taskName, "/F"], { quiet: true });
  } catch {
    // The task may not exist.
  }
  for (const target of [launcherPath, wrapperPath]) {
    try {
      if (existsSync(target)) unlinkSync(target);
    } catch {
      // The launcher may already be gone, or a concurrent uninstall removed it.
    }
  }
  process.stdout.write(`${JSON.stringify({ installed: false })}\n`);
} else if (command === "status") {
  let installed = false;
  let state = "stopped";
  let loaded = false;
  try {
    schtasks(["/Query", "/TN", taskName, "/FO", "LIST", "/V"]);
    installed = true;
    state = "ready";
    if (taskState() === "running") {
      try {
        loaded = Boolean(serviceProcessPid({ cleanup: false }));
      } catch {
        // An unavailable or failed process query is not proof that the service is alive.
      }
      if (loaded) state = "running";
    }
  } catch {
    // Missing task.
  }
  process.stdout.write(
    `${JSON.stringify({ installed, loaded, state })}\n`,
  );
} else if (command === "stop") {
  // Stopping is idempotent, like uninstall and restart: a task that is missing
  // or already idle is the state the caller asked for, not an error to raise.
  endTask();
  process.stdout.write(`${JSON.stringify({ state: "stopped" })}\n`);
} else {
  if (command === "restart") endTask();
  schtasks(["/Run", "/TN", taskName], { quiet: true });
  // 就绪等待内聚到服务管理器：直接调用 restart/start 也会等到 /health 通过才
  // 返回成功，避免调用方在路由器尚未接管端口时就继续发流量。
  ensureFetchProxyEnvironment();
  const health = await waitForRouterHealth({ timeoutMs: READINESS_TIMEOUT_MS });
  if (!health.ok) {
    process.stdout.write(
      `${JSON.stringify({ state: "running", ready: false, error: health.error })}\n`,
    );
    process.exit(1);
  }
  process.stdout.write(`${JSON.stringify({ state: "running", ready: true })}\n`);
}
