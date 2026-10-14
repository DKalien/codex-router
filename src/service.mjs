import { spawn } from "node:child_process";
import path from "node:path";

import { SOURCE_ROOT } from "./paths.mjs";
import { waitForRouterHealth } from "./router-health.mjs";
import { withServiceOperationLock } from "./service-operation-lock.mjs";

const platform = process.env.CODEX_ROUTER_SERVICE_PLATFORM || process.platform;
const script = {
  darwin: "service-macos.mjs",
  linux: "service-linux.mjs",
  win32: "service-windows.mjs",
}[platform];

if (!script) {
  throw new Error(`Unsupported background-service platform: ${platform}`);
}

const command = process.argv[2] || "status";
const mutatingCommands = new Set(["install", "uninstall", "start", "stop", "restart"]);
const readinessCommands = new Set(["install", "start", "restart"]);
// 覆盖路由器的 30 秒启动预算及系统服务拉起开销。
const READINESS_TIMEOUT_MS = 60_000;

async function runServiceCommand() {
  // 异步等待让跨进程操作锁持续续期，避免长操作被误判为陈旧锁。
  const status = await new Promise((resolve, reject) => {
    const child = spawn(
      process.execPath,
      [path.join(SOURCE_ROOT, "src", script), ...process.argv.slice(2)],
      { stdio: "inherit", env: process.env, windowsHide: true },
    );
    child.once("error", reject);
    child.once("close", (code) => resolve(code ?? 1));
  });
  if (status !== 0) return status;
  if (!readinessCommands.has(command)) return 0;
  // Windows 的 start/restart 已在管理器内检查就绪；install 仍由这里检查。
  if (platform === "win32" && command !== "install") return 0;

  const health = await waitForRouterHealth({ timeoutMs: READINESS_TIMEOUT_MS });
  if (health.ok) return 0;
  console.error(
    `Router did not become healthy within ${READINESS_TIMEOUT_MS / 1_000} seconds: ${health.error}`,
  );
  return 1;
}

try {
  const status = mutatingCommands.has(command)
    ? await withServiceOperationLock(runServiceCommand)
    : await runServiceCommand();
  process.exit(status);
} catch (error) {
  console.error(error instanceof Error ? error.message : String(error));
  process.exit(1);
}
