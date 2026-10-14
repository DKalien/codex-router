import { mkdirSync, readFileSync, renameSync, unlinkSync, writeFileSync } from "node:fs";
import path from "node:path";
import { randomUUID } from "node:crypto";
import lockfile from "proper-lockfile";
import { fileURLToPath } from "node:url";

import { STATE_DIR } from "./paths.mjs";
import { protectPrivateFileAsync } from "./file-security.mjs";
import { assertStateOwnership } from "./state-owner.mjs";
import { PROVIDERS } from "./model-registry.mjs";
import { readProviderSelection } from "./provider-selection.mjs";
import { resolveProviderCredential } from "./provider-credentials.mjs";

export const GPT_ROUTE_PATH = path.join(STATE_DIR, "gpt-route.json");
export const GPT_ROUTES = new Map([
  ["official", { label: "官方" }],
  ["wlb", { label: "WLB", provider: "wlb-relay" }],
  ["supergpt", { label: "SuperGPT", provider: "supergpt-relay" }],
]);

export function readGptRouteSnapshot() {
  let text;
  try {
    text = readFileSync(GPT_ROUTE_PATH, "utf8");
  } catch (error) {
    if (error.code === "ENOENT") return { route: "official", revision: null };
    throw new Error("无法读取 GPT 路由设置，请运行 route official、route wlb 或 route supergpt 修复。");
  }
  try {
    const value = JSON.parse(text);
    if (value.version === 1 && GPT_ROUTES.has(value.route) &&
        (value.revision === undefined || typeof value.revision === "string")) {
      return { route: value.route, revision: value.revision ?? null };
    }
  } catch {}
  // 路由状态损坏时停止请求，避免把请求意外发给另一家服务商。
  throw new Error("GPT 路由设置无效，请运行 route official、route wlb 或 route supergpt 修复。");
}

export function readGptRoute() {
  return readGptRouteSnapshot().route;
}

export async function setGptRoute(route, { expected, signal } = {}) {
  if (!GPT_ROUTES.has(route)) throw new Error("路由必须为 official、wlb 或 supergpt。");
  assertStateOwnership("切换 GPT 路由");
  mkdirSync(STATE_DIR, { recursive: true, mode: 0o700 });
  // 手动切换和自动回退使用同一把跨进程锁，检查版本与写入不能被另一写入插入。
  const release = await lockfile.lock(GPT_ROUTE_PATH, {
    realpath: false,
    stale: 90_000,
    retries: { retries: 20, factor: 1, minTimeout: 100, maxTimeout: 100 },
  });
  try {
    signal?.throwIfAborted();
    if (expected) {
      const current = readGptRouteSnapshot();
      if (current.route !== expected.route || current.revision !== expected.revision) return false;
    }
    const { provider, label } = GPT_ROUTES.get(route);
    if (provider) {
      if (!readProviderSelection().includes(provider)) {
        throw new Error(`${label} 尚未启用，请先配置并启用 ${provider}。`);
      }
      if (!resolveProviderCredential(PROVIDERS.get(provider))) {
        throw new Error(`${label} 未配置密钥，请先运行 provider-key ${provider} set。`);
      }
    }
    const temporary = `${GPT_ROUTE_PATH}.${process.pid}.tmp`;
    try {
      writeFileSync(temporary, `${JSON.stringify({ version: 1, route, revision: randomUUID() })}\n`, { mode: 0o600 });
      await protectPrivateFileAsync(temporary, { signal });
      signal?.throwIfAborted();
      renameSync(temporary, GPT_ROUTE_PATH);
    } finally {
      try { unlinkSync(temporary); } catch (error) { if (error.code !== "ENOENT") throw error; }
    }
    return route;
  } finally {
    await release();
  }
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  const command = process.argv[2] || "status";
  if (process.argv.length > 3 || (command !== "status" && !GPT_ROUTES.has(command))) {
    console.error("用法：route official|wlb|supergpt|status");
    process.exitCode = 2;
  } else {
    try {
      const route = command === "status" ? readGptRoute() : await setGptRoute(command);
      console.log(`GPT 路由：${GPT_ROUTES.get(route).label} (${route})`);
      if (command !== "status") console.log("设置已保存；支持此开关的路由器从下一次非子代理的原生 GPT 请求起使用新路由，所有任务共享此设置；已识别的原生 GPT 子代理固定走官方。");
    } catch (error) {
      console.error(error.message);
      process.exitCode = 1;
    }
  }
}
