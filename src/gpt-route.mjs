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
export const PI_ROUTE_PATH = path.join(STATE_DIR, "pi-route.json");
export const GPT_ROUTES = new Map([
  ["official", { label: "官方" }],
  ["wlb", { label: "WLB", provider: "wlb-relay" }],
  ["supergpt", { label: "SuperGPT", provider: "supergpt-relay" }],
]);
GPT_ROUTES.set("auto", { label: "自动（WLB → 官方 → SuperGPT）" });
export const SUBAGENT_ROUTES = new Map(GPT_ROUTES);
SUBAGENT_ROUTES.set("auto", { label: "自动（GPT → SuperGPT）" });
// Pi 接入当前只支持手动路由：官方额度查询针对 Codex 身份，不能拿来
// 为 Pi 的 OpenAI 凭据做自动降级决策。
export const PI_ROUTES = new Map([
  ["official", GPT_ROUTES.get("official")],
  ["wlb", GPT_ROUTES.get("wlb")],
  ["supergpt", GPT_ROUTES.get("supergpt")],
]);

function readRouteSnapshot(statePath, routes, description, repairCommand) {
  const routePrefix = `route ${repairCommand === "official" ? "" : `${repairCommand.replace(/ official$/, "")} `}`;
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

export function readPiRouteSnapshot() {
  return readRouteSnapshot(PI_ROUTE_PATH, PI_ROUTES, " Pi 路由设置", "pi official");
}

export function readGptRoute() {
  return readGptRouteSnapshot().route;
}

export function readSubagentRoute() {
  return readSubagentRouteSnapshot().route;
}

export function readPiRoute() {
  return readPiRouteSnapshot().route;
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
    const required = route === "auto"
      ? (routes === SUBAGENT_ROUTES ? ["supergpt"] : ["wlb", "supergpt"])
      : [route];
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

export function setPiRoute(route, options) {
  return setRoute(PI_ROUTE_PATH, PI_ROUTES, route, "Pi 路由", "pi official", options);
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  const args = process.argv.slice(2);
  const scope = args[0] === "subagent" || args[0] === "pi" ? args[0] : undefined;
  const command = scope ? (args[1] || "status") : (args[0] || "status");
  const expectedLength = scope ? (args.length === 1 ? 1 : 2) : (args.length === 0 ? 0 : 1);
  const routes = scope === "pi" ? PI_ROUTES : scope === "subagent" ? SUBAGENT_ROUTES : GPT_ROUTES;
  const label = scope === "pi" ? "Pi 路由" : scope === "subagent" ? "子代理供应商" : "GPT 路由";
  if (args.length !== expectedLength || (command !== "status" && !routes.has(command))) {
    console.error("用法：route official|wlb|supergpt|auto|status；route subagent official|wlb|supergpt|auto|status；route pi official|wlb|supergpt|status");
    process.exitCode = 2;
  } else {
    try {
      const read = scope === "pi" ? readPiRoute : scope === "subagent" ? readSubagentRoute : readGptRoute;
      const set = scope === "pi" ? setPiRoute : scope === "subagent" ? setSubagentRoute : setGptRoute;
      const route = command === "status" ? read() : await set(command);
      console.log(`${label}：${routes.get(route).label} (${route})`);
      if (command !== "status") {
        console.log(scope === "pi"
          ? "设置已保存；Pi 的 GPT 请求从下一次起使用新路由，所有任务共享此设置。"
          : scope === "subagent"
            ? "设置已保存；已识别的原生 GPT 子代理从下一次请求起使用该供应商，所有任务共享此设置。"
            : "设置已保存；支持此开关的路由器从下一次非子代理的原生 GPT 请求起使用新路由，所有任务共享此设置。");
      }
    } catch (error) {
      console.error(error.message);
      process.exitCode = 1;
    }
  }
}
