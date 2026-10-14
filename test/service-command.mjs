import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { EventEmitter } from "node:events";
import { readFileSync } from "node:fs";
import path from "node:path";
import test from "node:test";

const source = readFileSync(new URL("../src/service.mjs", import.meta.url), "utf8");
const body = source.slice(source.indexOf("async function runServiceCommand()"), source.indexOf("\ntry {"));
const readinessCommands = new Set(["install", "start", "restart"]);

function commandRun(platform, command, spawnImpl, health) {
  return new Function("spawn", "path", "SOURCE_ROOT", "script", "process", "command", "platform", "readinessCommands", "waitForRouterHealth", "READINESS_TIMEOUT_MS", "console", body + "; return runServiceCommand;")(
    spawnImpl, path, "/fixture", "service-fixture.mjs", { execPath: process.execPath, argv: ["node", "service", command], env: {} }, command, platform, readinessCommands, health, 60_000, { error() {} },
  );
}

function childResult(code, error) {
  return (_executable, _args, options) => {
    assert.equal(options.windowsHide, true);
    const child = new EventEmitter();
    setImmediate(() => child.emit(error ? "error" : "close", error || code));
    return child;
  };
}

test("服务操作等待子进程时保持事件循环可用", async () => {
  let timerRan = false;
  let child;
  const run = commandRun("win32", "restart", () => {
    child = spawn(process.execPath, ["-e", "setTimeout(() => process.exit(0), 100)"], { stdio: "ignore", windowsHide: true });
    return child;
  }, () => assert.fail("Windows restart 不应重复探测"));
  const timer = setTimeout(() => { timerRan = true; }, 10);
  try {
    assert.equal(await run(), 0);
    assert.equal(timerRan, true, "等待服务管理器时必须允许操作锁续期");
  } finally {
    clearTimeout(timer);
    if (child && child.exitCode === null && child.signalCode === null) child.kill();
  }
});

test("Windows 启停不重复探测，安装与其他平台保留就绪门禁", async () => {
  assert.match(source, /const READINESS_TIMEOUT_MS = 60_000;/);
  for (const [platform, command, expected] of [["win32", "start", 0], ["win32", "restart", 0], ["win32", "install", 1], ["linux", "restart", 1], ["darwin", "start", 1], ["win32", "stop", 0]]) {
    let calls = 0;
    const run = commandRun(platform, command, childResult(0), async (options) => {
      calls += 1;
      assert.equal(options.timeoutMs, 60_000);
      return { ok: true };
    });
    assert.equal(await run(), 0);
    assert.equal(calls, expected);
  }
});

test("服务启动失败或健康失败不能报告成功", async () => {
  for (const [code, expected] of [[7, 7], [null, 1]]) {
    assert.equal(await commandRun("win32", "restart", childResult(code), () => assert.fail("失败不得探测"))(), expected);
  }
  await assert.rejects(commandRun("win32", "restart", childResult(null, new Error("spawn failed")), () => {})(), /spawn failed/);
  assert.equal(await commandRun("linux", "start", childResult(0), async () => ({ ok: false, error: "fixture" }))(), 1);
});
