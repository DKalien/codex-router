import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import test from "node:test";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");

for (const scenario of ["spawn-error", "lost-exit"]) {
  test(`监督器处理 ${scenario}`, { timeout: 10000 }, async () => {
    const dir = mkdtempSync(path.join(os.tmpdir(), "router-lifecycle-"));
    const preload = path.join(dir, "preload.mjs");
    writeFileSync(path.join(dir, "caller-secret"), "test-caller-secret-with-sufficient-length");
    writeFileSync(preload, `
      import childProcess from "node:child_process";
      import { EventEmitter } from "node:events";
      import { syncBuiltinESMExports } from "node:module";
      childProcess.spawn = () => {
        const child = Object.assign(new EventEmitter(), { exitCode: null, signalCode: null, kill: () => true });
        if (${JSON.stringify(scenario)} === "spawn-error") queueMicrotask(() => child.emit("error", new Error("模拟启动失败")));
        else { child.pid = 123; setTimeout(() => process.emit("SIGTERM"), 100); }
        return child;
      };
      globalThis.fetch = async () => ({ ok: true, json: async () => ({ service: "codex-router" }) });
      const originalTimeout = globalThis.setTimeout;
      globalThis.setTimeout = (fn, ms, ...args) => originalTimeout(fn, ms >= 5000 ? 30 : ms, ...args);
      syncBuiltinESMExports();
    `);
    try {
      const child = spawn(process.execPath, ["--import", pathToFileURL(preload).href, path.join(root, "src/start.mjs")], {
        env: { ...process.env, CODEX_HOME: dir, MODEL_ROUTER_STATE_DIR: dir, CODEX_ROUTER_SERVICE_PID_PATH: path.join(dir, "service.pid") },
        stdio: ["ignore", "ignore", "pipe"],
        windowsHide: true,
      });
      let stderr = "";
      child.stderr.setEncoding("utf8");
      child.stderr.on("data", (chunk) => { stderr += chunk; });
      const [code] = await new Promise((resolve, reject) => {
        child.once("error", reject);
        child.once("close", (...args) => resolve(args));
      });
      assert.equal(code, 1, stderr);
      assert.match(stderr, scenario === "spawn-error" ? /模拟启动失败/ : /子进程停止后未报告退出/);
      assert.doesNotMatch(stderr, /Unhandled 'error' event/);
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  });
}
