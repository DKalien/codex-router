import { mkdirSync, readFileSync, renameSync, unlinkSync, writeFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { STATE_DIR } from "./paths.mjs";
import { protectPrivateFile } from "./file-security.mjs";
import { assertStateOwnership } from "./state-owner.mjs";
import { PROVIDERS } from "./model-registry.mjs";
import { readProviderSelection } from "./provider-selection.mjs";
import { resolveProviderCredential } from "./provider-credentials.mjs";

export const GPT_ROUTE_PATH = path.join(STATE_DIR, "gpt-route.json");

export function readGptRoute() {
  let text;
  try {
    text = readFileSync(GPT_ROUTE_PATH, "utf8");
  } catch (error) {
    if (error.code === "ENOENT") return "official";
    throw new Error("无法读取 GPT 路由设置，请运行 route official 或 route wlb 修复。");
  }
  try {
    const value = JSON.parse(text);
    if (value.version === 1 && ["official", "wlb"].includes(value.route)) return value.route;
  } catch {}
  // 路由状态损坏时停止请求，避免把请求意外发给另一家服务商。
  throw new Error("GPT 路由设置无效，请运行 route official 或 route wlb 修复。");
}

export function setGptRoute(route) {
  if (!["official", "wlb"].includes(route)) throw new Error("路由必须为 official 或 wlb。");
  assertStateOwnership("切换 GPT 路由");
  if (route === "wlb") {
    if (!readProviderSelection().includes("wlb-relay")) {
      throw new Error("WLB 尚未启用，请先配置并启用 wlb-relay。");
    }
    if (!resolveProviderCredential(PROVIDERS.get("wlb-relay"))) {
      throw new Error("WLB 未配置密钥，请先运行 provider-key wlb-relay set。");
    }
  }
  mkdirSync(STATE_DIR, { recursive: true, mode: 0o700 });
  const temporary = `${GPT_ROUTE_PATH}.${process.pid}.tmp`;
  try {
    writeFileSync(temporary, `${JSON.stringify({ version: 1, route })}\n`, { mode: 0o600 });
    protectPrivateFile(temporary);
    renameSync(temporary, GPT_ROUTE_PATH);
  } finally {
    try { unlinkSync(temporary); } catch (error) { if (error.code !== "ENOENT") throw error; }
  }
  return route;
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  const command = process.argv[2] || "status";
  if (process.argv.length > 3 || !["official", "wlb", "status"].includes(command)) {
    console.error("用法：route official|wlb|status");
    process.exitCode = 2;
  } else {
    try {
      const route = command === "status" ? readGptRoute() : setGptRoute(command);
      console.log(`GPT 路由：${route === "official" ? "官方" : "WLB"} (${route})`);
      if (command !== "status") console.log("设置已保存；支持此开关的路由器从下一次 GPT 请求起使用新路由，所有任务共享此设置。");
    } catch (error) {
      console.error(error.message);
      process.exitCode = 1;
    }
  }
}
