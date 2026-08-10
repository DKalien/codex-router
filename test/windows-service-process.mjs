import assert from "node:assert/strict";
import { spawn, spawnSync } from "node:child_process";
import { existsSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
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
