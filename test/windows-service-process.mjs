import assert from "node:assert/strict";
import { spawn, spawnSync } from "node:child_process";
import {
  chmodSync,
  existsSync,
  mkdirSync,
  mkdtempSync,
  readFileSync,
  rmSync,
  writeFileSync,
} from "node:fs";
import http from "node:http";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

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
      "if (command.includes(\"Get-ScheduledTask\")) process.stdout.write(process.env.CODEX_ROUTER_TEST_TASK_STATE || \"Ready\");",
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

      let result = spawnSync(
        process.execPath,
        [path.join(root, "src", "service-windows.mjs"), "stop"],
        { cwd: root, env, encoding: "utf8", timeout: 30_000 },
      );
      assert.equal(result.status, 0, result.stderr);
      await waitFor(() => service.exitCode !== null || service.signalCode !== null);
      assert.equal(existsSync(pidPath), false);

      writeFileSync(pidPath, `${unrelated.pid}\n`);
      result = spawnSync(
        process.execPath,
        [path.join(root, "src", "service-windows.mjs"), "stop"],
        { cwd: root, env, encoding: "utf8", timeout: 30_000 },
      );
      assert.equal(result.status, 0, result.stderr);
      assert.equal(unrelated.exitCode, null, "an unrelated Node process was terminated");
    } finally {
      await stopChild(service);
      await stopChild(unrelated);
      rmSync(stateDir, { recursive: true, force: true });
    }
  },
);
