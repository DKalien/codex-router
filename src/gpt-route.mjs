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
export const SUBAGENT_ROUTE_PATH = path.join(STATE_DIR, "subagent-route.json");
export const GPT_ROUTES = new Map([
  ["official", { label: "官方" }],
  ["wlb", { label: "WLB", provider: "wlb-relay" }],
  ["supergpt", { label: "SuperGPT", provider: "supergpt-relay" }],
]);
export const SUBAGENT_ROUTES = new Map(GPT_ROUTES);
GPT_ROUTES.set("auto", { label: "自动（WLB → 官方 → SuperGPT）" });

function readRouteSnapshot(statePath, routes, description, repairCommand) {
  const routePrefix = repairCommand.startsWith("subagent ") ? "route subagent " : "route ";
  let text;
  try {
    text = readFileSync(statePath, "utf8");
  } catch (error) {
    if (error.code === "ENOENT") return { route: "official", revision: null };
    throw new Error(`无法读取${description}，请运行 ${routePrefix}official、${routePrefix}wlb 或 ${routePrefix}supergpt 修复。`);
  }
  try {
    const value = JSON.parse(text);
    if (value.version === 1 && routes.has(value.route) &&
        (value.revision === undefined || typeof value.revision === "string")) {
      return { route: value.route, revision: value.revision ?? null };
    }
  } catch {}
  // 路由状态损坏时停止请求，避免把请求意外发给另一家服务商。
  throw new Error(`${description}无效，请运行 ${routePrefix}official、${routePrefix}wlb 或 ${routePrefix}supergpt 修复。`);
}

export function readGptRouteSnapshot() {
  return readRouteSnapshot(GPT_ROUTE_PATH, GPT_ROUTES, " GPT 路由设置", "official");
}

export function readSubagentRouteSnapshot() {
  return readRouteSnapshot(SUBAGENT_ROUTE_PATH, SUBAGENT_ROUTES, " 子代理供应商路由设置", "subagent official");
}

export function readGptRoute() {
  return readGptRouteSnapshot().route;
}

export function readSubagentRoute() {
  return readSubagentRouteSnapshot().route;
}

async function setRoute(statePath, routes, route, description, repairCommand, { expected, signal } = {}) {
  if (!routes.has(route)) throw new Error(`路由必须为 ${[...routes.keys()].join("、")}。`);
  assertStateOwnership(`切换${description}`);
  mkdirSync(STATE_DIR, { recursive: true, mode: 0o700 });
  // 手动切换和自动回退使用同一把跨进程锁，检查版本与写入不能被另一写入插入。
  const release = await lockfile.lock(statePath, {
    realpath: false,
    stale: 90_000,
    retries: { retries: 20, factor: 1, minTimeout: 100, maxTimeout: 100 },
  });
  try {
    signal?.throwIfAborted();
    if (expected) {
      const current = readRouteSnapshot(statePath, routes, ` ${description}设置`, repairCommand);
      if (current.route !== expected.route || current.revision !== expected.revision) return false;
    }
    const required = route === "auto" ? ["wlb", "supergpt"] : [route];
    for (const choice of required) {
      const { provider, label } = routes.get(choice);
      if (!provider) continue;
      if (!readProviderSelection().includes(provider)) {
        throw new Error(`${label} 尚未启用，请先配置并启用 ${provider}。`);
      }
      if (!resolveProviderCredential(PROVIDERS.get(provider))) {
        throw new Error(`${label} 未配置密钥，请先运行 provider-key ${provider} set。`);
      }
    }
    const temporary = `${statePath}.${process.pid}.tmp`;
    try {
      writeFileSync(temporary, `${JSON.stringify({ version: 1, route, revision: randomUUID() })}\n`, { mode: 0o600 });
      await protectPrivateFileAsync(temporary, { signal });
      signal?.throwIfAborted();
      renameSync(temporary, statePath);
    } finally {
      try { unlinkSync(temporary); } catch (error) { if (error.code !== "ENOENT") throw error; }
    }
    return route;
  } finally {
    await release();
  }
}

export function setGptRoute(route, options) {
  return setRoute(GPT_ROUTE_PATH, GPT_ROUTES, route, "GPT 路由", "official", options);
}

export function setSubagentRoute(route, options) {
  return setRoute(SUBAGENT_ROUTE_PATH, SUBAGENT_ROUTES, route, "子代理供应商", "subagent official", options);
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  const args = process.argv.slice(2);
  const isSubagent = args[0] === "subagent";
  const command = isSubagent ? (args[1] || "status") : (args[0] || "status");
  const expectedLength = isSubagent ? (args.length === 1 ? 1 : 2) : (args.length === 0 ? 0 : 1);
  const routes = isSubagent ? SUBAGENT_ROUTES : GPT_ROUTES;
  if (args.length !== expectedLength || (command !== "status" && !routes.has(command))) {
    console.error("用法：route official|wlb|supergpt|auto|status；route subagent official|wlb|supergpt|status");
    process.exitCode = 2;
  } else {
    try {
      const read = isSubagent ? readSubagentRoute : readGptRoute;
      const set = isSubagent ? setSubagentRoute : setGptRoute;
      const route = command === "status" ? read() : await set(command);
      console.log(`${isSubagent ? "子代理供应商" : "GPT 路由"}：${routes.get(route).label} (${route})`);
      if (command !== "status") {
        console.log(isSubagent
          ? "设置已保存；已识别的原生 GPT 子代理从下一次请求起使用该供应商，所有任务共享此设置。"
          : "设置已保存；支持此开关的路由器从下一次非子代理的原生 GPT 请求起使用新路由，所有任务共享此设置。");
      }
    } catch (error) {
      console.error(error.message);
      process.exitCode = 1;
    }
  }
}
