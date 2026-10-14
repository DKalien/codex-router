import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { once } from "node:events";
import { existsSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import http from "node:http";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { fileURLToPath, pathToFileURL } from "node:url";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");

async function exerciseProxy(overrides) {
  const stateDir = mkdtempSync(path.join(os.tmpdir(), "codex-router-start-proxy-"));
  const allocator = http.createServer();
  allocator.listen(0, "127.0.0.1");
  await once(allocator, "listening");
  const port = allocator.address().port;
  await new Promise((resolve) => allocator.close(resolve));
  const snapshotPath = path.join(stateDir, "environment.json");
  const probePath = path.join(stateDir, "proxy-probes");
  const timerPath = path.join(stateDir, "watchdog-timers");
  const stopPath = path.join(stateDir, "stop-stub");
  const stubPath = path.join(stateDir, "router-stub.mjs");
  const preloadPath = path.join(stateDir, "preload.mjs");
  writeFileSync(path.join(stateDir, "caller-secret"), "test-caller-secret-with-sufficient-length");
  writeFileSync(stubPath, `
    import http from "node:http";
    import { existsSync, writeFileSync } from "node:fs";
    writeFileSync(${JSON.stringify(snapshotPath)}, JSON.stringify({ HTTP_PROXY: process.env.HTTP_PROXY, HTTPS_PROXY: process.env.HTTPS_PROXY, NODE_USE_ENV_PROXY: process.env.NODE_USE_ENV_PROXY, NO_PROXY: process.env.NO_PROXY, http_proxy: process.env.http_proxy, https_proxy: process.env.https_proxy, no_proxy: process.env.no_proxy, childPid: process.pid }));
    const server = http.createServer((req, res) => {
      writeFileSync(${JSON.stringify(path.join(stateDir, "health-served"))}, req.url);
      res.writeHead(200, { "Content-Type": "application/json" });
      res.end(JSON.stringify({ service: "codex-router", router: "ready" }));
    });
    server.listen(${port}, "127.0.0.1");
    const timer = setInterval(() => {
      if (existsSync(${JSON.stringify(stopPath)})) { clearInterval(timer); server.close(); }
    }, 20);
    setTimeout(() => process.exit(2), 10000).unref();
  `);
  writeFileSync(preloadPath, `
    import childProcess from "node:child_process";
    import net from "node:net";
    import { EventEmitter } from "node:events";
    import { appendFileSync } from "node:fs";
    import { syncBuiltinESMExports } from "node:module";
    const spawn = childProcess.spawn;
    childProcess.spawn = (command, args, options) => spawn(command, [${JSON.stringify(stubPath)}], options);
    const connect = net.connect;
    net.connect = (...args) => {
      if (Number(args[0]?.port) === ${port}) return connect(...args);
      appendFileSync(${JSON.stringify(probePath)}, "probe\\n");
      const socket = new EventEmitter();
      socket.destroy = () => {};
      socket.setTimeout = () => socket;
      queueMicrotask(() => socket.emit("error", new Error("test proxy unavailable")));
      return socket;
    };
    const setTimeout = globalThis.setTimeout;
    globalThis.setTimeout = (fn, ms, ...args) => {
      if (ms === 12000) appendFileSync(${JSON.stringify(timerPath)}, "watchdog\\n");
      return setTimeout(fn, ms, ...args);
    };
    syncBuiltinESMExports();
  `);
  const env = { ...process.env };
  for (const key of Object.keys(env)) {
    if (["https_proxy", "http_proxy", "no_proxy", "node_options", "model_router_port", "codex_router_port"].includes(key.toLowerCase())) delete env[key];
  }
  const service = spawn(process.execPath, ["--import", pathToFileURL(preloadPath).href, path.join(root, "src/start.mjs")], {
    cwd: root,
    env: { ...env, MODEL_ROUTER_TARGET: "codex", CODEX_HOME: stateDir, MODEL_ROUTER_STATE_DIR: stateDir, CODEX_ROUTER_PORT: String(port), CODEX_ROUTER_QUIET: "1", NODE_USE_ENV_PROXY: "0", NO_PROXY: "example.test", ...overrides },
    stdio: ["ignore", "ignore", "pipe"],
  });
  const exited = once(service, "exit");
  let completed = false;
  let stderr = "";
  service.stderr.on("data", (chunk) => { stderr += chunk; });
  try {
    const deadline = Date.now() + 8000;
    while (!stderr.includes("ready (authenticated loopback endpoint)")) {
      assert.equal(service.exitCode, null, stderr);
      assert.ok(Date.now() < deadline, `start did not become ready: ${stderr}`);
      await new Promise((resolve) => setTimeout(resolve, 20));
    }
    assert.equal(readFileSync(path.join(stateDir, "health-served"), "utf8"), "/health");
    const snapshot = JSON.parse(readFileSync(snapshotPath, "utf8"));
    assert.equal(existsSync(probePath), false, "start must not probe proxy ports");
    assert.equal(existsSync(timerPath), false, "start must not schedule the proxy watchdog");
    writeFileSync(stopPath, "stop");
    const [code, signal] = await exited;
    assert.equal(code, 0, stderr);
    assert.equal(signal, null);
    assert.equal(existsSync(path.join(stateDir, "service.pid")), false);
    completed = true;
    return snapshot;
  } finally {
    if (service.exitCode === null && service.signalCode === null) { service.kill(); await exited; }
    if (!completed && existsSync(snapshotPath)) {
      const { childPid } = JSON.parse(readFileSync(snapshotPath, "utf8"));
      try { process.kill(childPid); } catch (error) { if (error.code !== "ESRCH") throw error; }
    }
    rmSync(stateDir, { recursive: true, force: true });
  }
}

test("start 固定代理无需监听，显式代理依次优先，直连名单保留用户和必要主机", async () => {
  for (const [overrides, expected] of [
    [{}, "http://127.0.0.1:7897"],
    [{ HTTPS_PROXY: "http://127.0.0.1:18001", HTTP_PROXY: "http://127.0.0.1:18002" }, "http://127.0.0.1:18001"],
    [{ https_proxy: "http://127.0.0.1:18003", HTTP_PROXY: "http://127.0.0.1:18002" }, "http://127.0.0.1:18003"],
    [{ HTTP_PROXY: "http://127.0.0.1:18002", http_proxy: "http://127.0.0.1:18004" }, "http://127.0.0.1:18002"],
    [{ http_proxy: "http://127.0.0.1:18004" }, "http://127.0.0.1:18004"],
  ]) {
    const snapshot = await exerciseProxy(overrides);
    assert.equal(snapshot.HTTPS_PROXY, expected);
    assert.equal(snapshot.HTTP_PROXY, expected);
    assert.equal(snapshot.http_proxy, expected);
    assert.equal(snapshot.https_proxy, expected);
    assert.equal(snapshot.no_proxy, snapshot.NO_PROXY);
    assert.equal(snapshot.NODE_USE_ENV_PROXY, "1");
    const direct = snapshot.NO_PROXY.split(",");
    for (const host of ["example.test", "127.0.0.1", "localhost", "codex.wlbclub.com", "token-plan-cn.xiaomimimo.com", "api.supergpt.dev"]) assert.ok(direct.includes(host), `NO_PROXY missing ${host}`);
  }
});
