import assert from "node:assert/strict";
import { mkdtempSync, readFileSync, readdirSync, rmSync, writeFileSync } from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";

const sandbox = mkdtempSync(path.join(os.tmpdir(), "codex-router-route-async-"));
process.env.MODEL_ROUTER_STATE_DIR = sandbox;
process.env.CODEX_HOME = path.join(sandbox, "codex-home");
const {
  GPT_ROUTE_PATH,
  SUBAGENT_ROUTE_PATH,
  readGptRouteSnapshot,
  readSubagentRouteSnapshot,
  setGptRoute,
  setSubagentRoute,
} = await import("../src/gpt-route.mjs");
const { privateFileIsProtected } = await import("../src/file-security.mjs");
test.after(() => rmSync(sandbox, { recursive: true, force: true }));

function assertClean() {
  assert.equal(readdirSync(sandbox).some((name) => name.endsWith(".tmp") || name.endsWith(".lock")), false);
}

test("同路由重选保持原子写入、私有权限并更新 revision，ACL 等待不阻塞事件循环", async () => {
  await setGptRoute("official");
  const previous = readGptRouteSnapshot();
  let pulses = 0;
  const timer = setInterval(() => {
    pulses++;
    const current = readGptRouteSnapshot();
    assert.equal(current.route, "official");
    assert.equal(typeof current.revision, "string");
  }, 10);
  try {
    assert.equal(await setGptRoute("official"), "official");
  } finally {
    clearInterval(timer);
  }
  assert.notEqual(readGptRouteSnapshot().revision, previous.revision);
  assert.equal(privateFileIsProtected(GPT_ROUTE_PATH), true);
  if (process.platform === "win32") assert.ok(pulses >= 2, `事件循环仅推进 ${pulses} 次`);
  assertClean();
});

test("并发 expected 切换在锁内复核，旧 revision 只能提交一次", async () => {
  const expected = readGptRouteSnapshot();
  const results = await Promise.all([
    setGptRoute("official", { expected }),
    setGptRoute("official", { expected }),
  ]);
  assert.deepEqual(results.sort(), [false, "official"].sort());
  assertClean();
});

test("子代理供应商使用独立状态文件并保持默认官方", async () => {
  assert.equal(readSubagentRouteSnapshot().route, "official");
  await setSubagentRoute("official");
  const snapshot = readSubagentRouteSnapshot();
  assert.equal(snapshot.route, "official");
  assert.equal(typeof snapshot.revision, "string");
  assert.equal(privateFileIsProtected(SUBAGENT_ROUTE_PATH), true);
  assert.equal(readGptRouteSnapshot().route, "official");
  assert.equal(privateFileIsProtected(GPT_ROUTE_PATH), true);
});

test("子代理状态损坏时提示独立修复命令", () => {
  writeFileSync(SUBAGENT_ROUTE_PATH, "broken");
  assert.throws(
    () => readSubagentRouteSnapshot(),
    /route subagent official、route subagent wlb 或 route subagent supergpt/,
  );
  rmSync(SUBAGENT_ROUTE_PATH, { force: true });
});

test("异步 ACL 期间取消保留旧路由并清理临时文件", { skip: process.platform !== "win32" }, async () => {
  const previous = readFileSync(GPT_ROUTE_PATH, "utf8");
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 30);
  try {
    await assert.rejects(setGptRoute("official", { signal: controller.signal }), { name: "AbortError" });
  } finally {
    clearTimeout(timer);
  }
  assert.equal(readFileSync(GPT_ROUTE_PATH, "utf8"), previous);
  assertClean();
});

test("缺少 pwsh 时兼容 Windows PowerShell；两个 shell 均失败时保留旧状态", { skip: process.platform !== "win32" }, async () => {
  const pathKey = Object.keys(process.env).find((key) => key.toLowerCase() === "path") || "PATH";
  const savedPath = process.env[pathKey];
  try {
    process.env[pathKey] = path.join(process.env.SystemRoot, "System32", "WindowsPowerShell", "v1.0");
    await setGptRoute("official");
    const previous = readFileSync(GPT_ROUTE_PATH, "utf8");
    process.env[pathKey] = sandbox;
    await assert.rejects(setGptRoute("official"), /Failed to protect private file ACL/);
    assert.equal(readFileSync(GPT_ROUTE_PATH, "utf8"), previous);
  } finally {
    if (savedPath === undefined) delete process.env[pathKey];
    else process.env[pathKey] = savedPath;
  }
  assert.equal(privateFileIsProtected(GPT_ROUTE_PATH), true);
  assertClean();
});
