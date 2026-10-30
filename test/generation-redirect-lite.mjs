import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { once } from "node:events";
import { mkdtempSync, rmSync } from "node:fs";
import http from "node:http";
import os from "node:os";
import path from "node:path";
import test from "node:test";

const key = "test-router-caller-capability-with-sufficient-length";
test("生成重定向不重放请求、不泄露地址，模型查询仍可重定向", { timeout: 30000 }, async () => {
  let origins = 0, targets = 0, status = 307;
  const target = http.createServer((req, res) => { targets++; res.end('{"models":[]}'); });
  target.listen(0, "127.0.0.1"); await once(target, "listening");
  const origin = http.createServer(async (req, res) => {
    for await (const _ of req) {};
    origins++;
    res.writeHead(status, { location: `http://127.0.0.1:${target.address().port}/private?token=secret` });
    res.end("private response");
  });
  origin.listen(0, "127.0.0.1"); await once(origin, "listening");
  const reserve = http.createServer(); reserve.listen(0, "127.0.0.1"); await once(reserve, "listening");
  const port = reserve.address().port; await new Promise(resolve => reserve.close(resolve));
  const dir = mkdtempSync(path.join(os.tmpdir(), "router-redirect-"));
  const upstream = `http://127.0.0.1:${origin.address().port}`;
  const child = spawn(process.execPath, ["src/router.mjs"], { cwd: process.cwd(), windowsHide: true,
    env: { ...process.env, CODEX_HOME: dir, MODEL_ROUTER_STATE_DIR: dir, CODEX_ROUTER_PORT: String(port),
      CODEX_ROUTER_CALLER_KEY: key, CODEX_ROUTER_QUIET: "1", NODE_USE_ENV_PROXY: "0",
      CODEX_NATIVE_BASE_URL: upstream, PI_NATIVE_BASE_URL: upstream, MIMO_BASE_URL: upstream, MIMO_API_KEY: "test-key" },
    stdio: ["ignore", "ignore", "pipe"] });
  try {
    let log = "";
    await new Promise((resolve, reject) => {
      const timer = setTimeout(() => reject(new Error(log)), 10000);
      child.stderr.on("data", chunk => { log += chunk; if (log.includes("listening")) { clearTimeout(timer); resolve(); } });
      child.once("exit", code => { clearTimeout(timer); reject(new Error(`exit ${code}: ${log}`)); });
    });
    const base = `http://127.0.0.1:${port}/_codex-router/${key}`;
    for (const [endpoint, model] of [["/v1/responses", "gpt-test"], ["/pi/v1/responses", "gpt-test"],
      ["/v1/responses", "mimo-token-plan/mimo-v2-flash"], ["/v1/responses/compact", "mimo-token-plan/mimo-v2-flash"],
      ["/v1/images/generations", "gpt-image-2"]]) {
      for (status of [301, 302, 303, 307, 308]) {
        const before = origins;
        const res = await fetch(base + endpoint, { method: "POST", redirect: "manual", headers: { authorization: "Bearer test", "content-type": "application/json" },
          body: JSON.stringify({ model, input: "test", stream: false }) });
        const body = await res.text();
        assert.equal(res.status, 400, `${endpoint}: ${body}`);
        assert.equal(res.headers.get("location"), null);
        assert.doesNotMatch(body, /private|secret/);
        assert.equal(origins, before + 1); assert.equal(targets, 0);
      }
    }
    const res = await fetch(base + "/v1/models", { headers: { authorization: "Bearer test" } });
    assert.equal(res.status, 200); assert.equal(targets, 1);
  } finally {
    child.kill("SIGTERM"); if (child.exitCode === null) await once(child, "exit");
    origin.closeAllConnections(); target.closeAllConnections();
    await Promise.all([new Promise(resolve => origin.close(resolve)), new Promise(resolve => target.close(resolve))]);
    rmSync(dir, { recursive: true, force: true });
  }
});
