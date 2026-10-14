import assert from "node:assert/strict";
import { execFileSync, spawn, spawnSync } from "node:child_process";
import {
  chmodSync,
  existsSync,
  mkdirSync,
  mkdtempSync,
  readFileSync,
  rmSync,
  unlinkSync,
  writeFileSync,
} from "node:fs";
import http from "node:http";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

import { waitForRouterHealth } from "../src/router-health.mjs";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");

async function unusedPort() {
  const server = http.createServer();
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  const port = server.address().port;
  await new Promise((resolve) => server.close(resolve));
  return port;
}

async function waitFor(predicate, timeoutMs = 10_000) {
  const deadline = Date.now() + timeoutMs;
  while (!predicate()) {
    if (Date.now() >= deadline) throw new Error("Timed out waiting for the service process.");
    await new Promise((resolve) => setTimeout(resolve, 50));
  }
}

async function stopChild(child) {
  if (child.exitCode !== null || child.signalCode !== null) return;
  child.kill("SIGTERM");
  await new Promise((resolve) => child.once("exit", resolve));
}

function writeExecutable(target, contents) {
  writeFileSync(target, contents);
  chmodSync(target, 0o755);
}

function schedulerStubs(directory, { taskState = "Ready", processPid = "" } = {}) {
  mkdirSync(directory, { recursive: true });
  writeExecutable(path.join(directory, "schtasks.exe"), "#!/bin/sh\nexit 0\n");
  writeExecutable(
    path.join(directory, "powershell.exe"),
    [
      "#!/usr/bin/env node",
      "const command = process.argv.at(-1) || \"\";",
      "if (command.includes(\"Schedule.Service\")) process.stdout.write(process.env.CODEX_ROUTER_TEST_TASK_STATE === \"Running\" ? \"4\" : \"3\");",
      "if (command.includes(\"Get-CimInstance\")) process.stdout.write(process.env.CODEX_ROUTER_TEST_PROCESS_PID || \"\");",
      "",
    ].join("\n"),
  );
  return {
    path: `${directory}${path.delimiter}${process.env.PATH || ""}`,
    taskState,
    processPid,
  };
}

function runWindowsStatus(root, stateDir, stubs) {
  return spawnSync(
    process.execPath,
    [path.join(root, "src", "service-windows.mjs"), "status"],
    {
      cwd: root,
      env: {
        ...process.env,
        CODEX_HOME: stateDir,
        MODEL_ROUTER_STATE_DIR: stateDir,
        CODEX_ROUTER_STATE_DIR: stateDir,
        CODEX_ROUTER_SERVICE_PLATFORM: "win32",
        CODEX_ROUTER_TASK_NAME: `Codex Router Test ${process.pid}-${Date.now()}`,
        CODEX_ROUTER_TEST_TASK_STATE: stubs.taskState,
        CODEX_ROUTER_TEST_PROCESS_PID: stubs.processPid,
        PATH: stubs.path,
      },
      encoding: "utf8",
      timeout: 30_000,
    },
  );
}

test(
  "Windows status requires a live service start.mjs process",
  { skip: process.platform === "win32" },
  () => {
    for (const [name, pid] of [
      ["missing", undefined],
      ["external", 999_999],
    ]) {
      const testRoot = mkdtempSync(path.join(os.tmpdir(), `codex-router-win-status-${name}-`));
      try {
        const stateDir = path.join(testRoot, "state");
        mkdirSync(stateDir, { recursive: true });
        if (pid) writeFileSync(path.join(stateDir, "service.pid"), `${pid}\n`);
        const stubs = schedulerStubs(path.join(testRoot, "scheduler"), {
          taskState: "Running",
        });
        const result = runWindowsStatus(root, stateDir, stubs);
        assert.equal(result.status, 0, result.stderr);
        assert.deepEqual(JSON.parse(result.stdout), {
          installed: true,
          loaded: false,
          state: "ready",
        });
      } finally {
        rmSync(testRoot, { recursive: true, force: true });
      }
    }
  },
);

test(
  "Windows status marks the service loaded only after process verification",
  { skip: process.platform === "win32" },
  () => {
    const testRoot = mkdtempSync(path.join(os.tmpdir(), "codex-router-win-status-live-"));
    try {
      const stateDir = path.join(testRoot, "state");
      mkdirSync(stateDir, { recursive: true });
      const pid = 42_424;
      writeFileSync(path.join(stateDir, "service.pid"), `${pid}\n`);
      const stubs = schedulerStubs(path.join(testRoot, "scheduler"), {
        taskState: "Running",
        processPid: String(pid),
      });
      const result = runWindowsStatus(root, stateDir, stubs);
      assert.equal(result.status, 0, result.stderr);
      assert.deepEqual(JSON.parse(result.stdout), {
        installed: true,
        loaded: true,
        state: "running",
      });
    } finally {
      rmSync(testRoot, { recursive: true, force: true });
    }
  },
);

test(
  "Windows stop terminates only the verified service process tree",
  { skip: process.platform !== "win32" },
  async () => {
    const stateDir = mkdtempSync(path.join(os.tmpdir(), "codex-router-service-pid-"));
    const pidPath = path.join(stateDir, "service.pid");
    const taskName = `Codex Router Test ${process.pid}-${Date.now()}`;
    const port = await unusedPort();
    const env = {
      ...process.env,
      CODEX_HOME: stateDir,
      MODEL_ROUTER_STATE_DIR: stateDir,
      CODEX_ROUTER_STATE_DIR: stateDir,
      CODEX_ROUTER_SERVICE_PID_PATH: pidPath,
      CODEX_ROUTER_SERVICE_PLATFORM: "win32",
      CODEX_ROUTER_TASK_NAME: taskName,
      CODEX_ROUTER_PORT: String(port),
      MODEL_ROUTER_PORT: String(port),
      CODEX_ROUTER_QUIET: "1",
      NO_PROXY: "127.0.0.1,localhost",
    };
    writeFileSync(
      path.join(stateDir, "caller-secret"),
      "test-caller-secret-with-sufficient-length",
    );

    const service = spawn(process.execPath, [path.join(root, "src", "start.mjs")], {
      cwd: root,
      env,
      stdio: "ignore",
    });
    const unrelated = spawn(process.execPath, ["-e", "setInterval(() => {}, 1000)"], {
      stdio: "ignore",
    });

    try {
      await waitFor(() => existsSync(pidPath));
      assert.equal(Number.parseInt(readFileSync(pidPath, "utf8"), 10), service.pid);
      // 等路由器就绪再停止：停止请求文件只对已安装 watcher 的实例生效，
      // 否则会被当作启动残留清理掉，测试就只会走强杀兜底。
      const health = await waitForRouterHealth({
        url: `http://127.0.0.1:${port}/health`,
        timeoutMs: 30_000,
      });
      assert.equal(health.ok, true, health.error);

      let result = spawnSync(
        process.execPath,
        [path.join(root, "src", "service-windows.mjs"), "stop"],
        { cwd: root, env, encoding: "utf8", timeout: 30_000 },
      );
      assert.equal(result.status, 0, result.stderr);
      await waitFor(() => service.exitCode !== null || service.signalCode !== null);
      assert.equal(service.exitCode, 0, "the service should exit cleanly after a graceful stop");
      assert.equal(existsSync(pidPath), false);
      assert.equal(existsSync(path.join(stateDir, "shutdown.request")), false);

      writeFileSync(pidPath, `${unrelated.pid}\n`);
      result = spawnSync(
        process.execPath,
        [path.join(root, "src", "service-windows.mjs"), "stop"],
        { cwd: root, env, encoding: "utf8", timeout: 30_000 },
      );
      assert.equal(result.status, 0, result.stderr);
      assert.equal(unrelated.exitCode, null, "an unrelated Node process was terminated");
      assert.equal(existsSync(path.join(stateDir, "shutdown.request")), false);
    } finally {
      await stopChild(service);
      await stopChild(unrelated);
      rmSync(stateDir, { recursive: true, force: true });
    }
  },
);


test("Windows service verification uses the registered Node and status preserves PID files", {
  skip: process.platform !== "win32",
}, () => {
  const stateDir = mkdtempSync(path.join(os.tmpdir(), "codex-router-service-exe-"));
  try {
    const launcherPath = path.join(stateDir, "start-codex-router-hidden.vbs");
    const wrapperPath = path.join(stateDir, "start-codex-router.cmd");
    const servicePidPath = path.join(stateDir, "service.pid");
    const startPath = path.join(root, "src", "start.mjs");
    const registeredNode = "C:\\Node24\\node.exe";
    const pid = 42_424;
    writeFileSync(servicePidPath, `${pid}\n`);
    const source = readFileSync(path.join(root, "src", "service-windows.mjs"), "utf8");
    const functions = source.slice(source.indexOf("function readServicePid()"), source.indexOf("function stopServiceProcess("));
    let commandLine = `"${registeredNode}" "${startPath}"`;
    let generatedScript;
    let actualNode = registeredNode;
    const query = (_executable, args, options) => {
      generatedScript = args.at(-1);
      const mock = "function Get-CimInstance { param($ClassName, $Filter, $ErrorAction) [pscustomobject]@{ ExecutablePath = $env:TEST_NODE; CommandLine = $env:TEST_COMMAND; ProcessId = 42424; ParentProcessId = 123; Name = 'node.exe' } }; ";
      return execFileSync("powershell.exe", ["-NoProfile", "-NonInteractive", "-Command", mock + generatedScript], {
        ...options,
        env: { ...options.env, TEST_NODE: actualNode, TEST_COMMAND: commandLine },
      });
    };
    const { findServiceProcess, serviceProcessPid } = new Function(
      "readFileSync", "unlinkSync", "existsSync", "path", "vbsEscape", "execFileSync", "launcherPath", "wrapperPath", "servicePidPath", "startPath", "TASK_STATE_TIMEOUT_MS",
      `${functions}; return { findServiceProcess, serviceProcessPid };`,
    )(readFileSync, unlinkSync, existsSync, path, (value) => value.replaceAll('"', '""'), query, launcherPath, wrapperPath, servicePidPath, startPath, 15_000);
    const launcher = spawnSync(process.execPath, [path.join(root, "src", "service-windows.mjs"), "render-launcher"], { encoding: "utf8" });
    assert.equal(launcher.status, 0, launcher.stderr);
    const contents = launcher.stdout.replaceAll(process.execPath, registeredNode);
    writeFileSync(launcherPath, Buffer.concat([Buffer.from([0xff, 0xfe]), Buffer.from(contents, "utf16le")]));
    assert.equal(findServiceProcess(pid), pid);
    assert.equal(serviceProcessPid({ cleanup: false }), pid);
    assert.equal(readFileSync(servicePidPath, "utf8"), `${pid}\n`);
    assert.match(source, /loaded = Boolean\(serviceProcessPid\(\{ cleanup: false \}\)\)/);
    actualNode = process.execPath;
    assert.equal(findServiceProcess(pid), undefined, "a different executable must not match");
    actualNode = registeredNode;
    commandLine = `"${registeredNode}" "${path.join(root, "src", "router.mjs")}"`;
    assert.equal(findServiceProcess(pid), undefined, "the exact start script is required");
    commandLine = `"${registeredNode}" "${startPath}" --extra`;
    assert.equal(serviceProcessPid({ cleanup: false }), undefined);
    assert.equal(readFileSync(servicePidPath, "utf8"), `${pid}\n`);
    assert.match(generatedScript, /ParentProcessId/);
    commandLine = `"${registeredNode}" "${startPath}"`;
    writeFileSync(launcherPath, 'status = shell.Run("cmd.exe /D /C " & quote & quote & "' + wrapperPath + '" & quote & quote, 0, True)');
    writeFileSync(wrapperPath, `@echo off\r\n"${registeredNode}" "${startPath}" >> "test.log" 2>&1\r\n`);
    assert.equal(findServiceProcess(pid), pid, "older wrapper launchers remain supported");
    writeFileSync(launcherPath, "unrecognized launcher");
    assert.throws(() => findServiceProcess(pid), /Unable to identify Node/);
    assert.equal(readFileSync(servicePidPath, "utf8"), `${pid}\n`);
  } finally {
    rmSync(stateDir, { recursive: true, force: true });
  }
});


test("Windows reinstall stops the previous launcher before replacing it and retains recovery", () => {
  const source = readFileSync(path.join(root, "src", "service-windows.mjs"), "utf8");
  const branch = source.slice(source.indexOf('} else if (command === "install") {') + 2, source.indexOf('} else if (command === "uninstall") {') + 1);
  for (const failWrite of [false, true]) {
    const calls = [];
    const execute = new Function("command", "endTask", "writeLaunchers", "installTask", "schtasks", "taskExists", "process", "wrapperPath", "taskName", branch.replace(/^else /, ""));
    execute("install", () => calls.push("stop-old"), () => {
      calls.push("write-new");
      if (failWrite) throw new Error("fixture write failed");
    }, () => calls.push("register"), () => calls.push("run"), () => true,
    { stdout: { write() {} } }, "fixture.cmd", "fixture-task");
    assert.deepEqual(calls, failWrite ? ["stop-old", "write-new", "run"] : ["stop-old", "write-new", "register", "run"]);
  }
});


function windowsServiceFunction(name, next) {
  const source = readFileSync(path.join(root, "src", "service-windows.mjs"), "utf8");
  return source.slice(source.indexOf(`function ${name}(`), source.indexOf(next, source.indexOf(`function ${name}(`)));
}

test("Windows task state uses COM and shares the deadline across shell fallback", () => {
  const body = windowsServiceFunction("taskState", "\n// Node ");
  let now = 100;
  const calls = [];
  const query = (executable, args, options) => {
    calls.push({ executable, timeout: options.timeout });
    assert.match(args.at(-1), /Schedule\.Service/);
    assert.doesNotMatch(args.at(-1), /Get-ScheduledTask/);
    if (executable === "pwsh.exe") {
      now += 60;
      throw new Error("fixture Core failure");
    }
    return "4";
  };
  const state = new Function("execFileSync", "taskName", "TASK_STATE_TIMEOUT_MS", "Date", `${body}; return taskState;`)(query, "fixture", 15_000, { now: () => now });
  assert.equal(state(200), "running");
  assert.deepEqual(calls, [{ executable: "pwsh.exe", timeout: 100 }, { executable: "powershell.exe", timeout: 40 }]);
  calls.length = 0;
  now = 100;
  assert.equal(state(150), undefined);
  assert.deepEqual(calls, [{ executable: "pwsh.exe", timeout: 50 }], "deadline exhaustion must skip fallback");
  for (const [output, expected] of [["0", "unknown"], ["1", "disabled"], ["2", "queued"], ["3", "ready"], ["missing", "missing"], ["", undefined], ["unexpected", undefined]]) {
    const read = new Function("execFileSync", "taskName", "TASK_STATE_TIMEOUT_MS", `${body}; return taskState;`)(() => output, "fixture", 15_000);
    assert.equal(read(), expected);
  }
});

test("Windows task stop has a strict deadline and fails closed on unknown state", () => {
  const body = windowsServiceFunction("waitForTaskToStop", "\nfunction readServicePid(");
  function wait(query) {
    let now = 0;
    const sleeps = [];
    const execute = new Function("taskState", "TASK_STOP_TIMEOUT_MS", "TASK_STOP_POLL_MS", "Date", "sleep", `${body}; return waitForTaskToStop;`)(
      (deadline) => query(deadline, (elapsed) => { now += elapsed; }), 100, 60, { now: () => now }, (ms) => { sleeps.push(ms); now += ms; },
    );
    return { execute, sleeps };
  }
  for (const state of [undefined, "unknown"]) assert.throws(wait(() => state).execute, /Unable to verify/);
  for (const state of ["missing", "ready", "disabled"]) assert.doesNotThrow(wait(() => state).execute);
  for (const state of ["running", "queued"]) {
    const fixture = wait(() => state);
    assert.throws(fixture.execute, /Timed out/);
    assert.deepEqual(fixture.sleeps, [60, 40]);
  }
  assert.throws(wait((deadline, advance) => { assert.equal(deadline, 100); advance(100); return "ready"; }).execute, /Timed out/, "a query returning after the deadline cannot report success");
});

test("Windows stop cleans verified orphans before waiting and install never runs after stop failure", () => {
  const body = windowsServiceFunction("endTask", "\n// Only a task");
  for (const endFails of [false, true]) {
    const calls = [];
    const stop = new Function("serviceProcessPid", "requestGracefulStop", "removeServicePid", "waitForTaskToStop", "schtasks", "taskName", "stopServiceProcess", `${body}; return endTask;`)(
      () => 42, () => false, () => {}, () => { calls.push("wait"); throw new Error("unknown state"); }, () => { calls.push("end"); if (endFails) throw new Error("missing task"); }, "fixture", (pid) => { assert.equal(pid, 42); calls.push("kill"); },
    );
    assert.throws(stop, /unknown state/);
    assert.deepEqual(calls, ["end", "kill", "wait"]);
  }
  const source = readFileSync(path.join(root, "src", "service-windows.mjs"), "utf8");
  const branch = source.slice(source.indexOf('} else if (command === "install") {') + 2, source.indexOf('} else if (command === "uninstall") {') + 1);
  const execute = new Function("command", "endTask", "schtasks", branch.replace(/^else /, ""));
  assert.throws(() => execute("install", () => { throw new Error("stop timed out"); }, () => assert.fail("must not run")), /stop timed out/);
  assert.match(windowsServiceFunction("schtasks", "\nfunction writeAtomic("), /timeout: TASK_STATE_TIMEOUT_MS/);
  assert.match(windowsServiceFunction("stopServiceProcess", "\nfunction isProcessAlive("), /timeout: TASK_STATE_TIMEOUT_MS/);
  const processQuery = windowsServiceFunction("findServiceProcess", "\nfunction serviceProcessPid(");
  assert.match(processQuery, /\["pwsh\.exe", "powershell\.exe"\]/);
});
