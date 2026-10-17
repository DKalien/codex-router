import { mkdirSync, readFileSync, renameSync, unlinkSync, writeFileSync } from "node:fs";
import path from "node:path";
import { randomUUID } from "node:crypto";
import lockfile from "proper-lockfile";
import { STATE_DIR } from "./paths.mjs";
import {
  GPT_ROUTE_PATH,
  SUBAGENT_ROUTE_PATH,
  readGptRouteSnapshot,
  readSubagentRouteSnapshot,
} from "./gpt-route.mjs";
import { assertStateOwnership } from "./state-owner.mjs";
import { protectPrivateFileAsync } from "./file-security.mjs";

export const AUTO_ROUTE_STATUS_PATH = path.join(STATE_DIR, "auto-route-status.json");
export const SUBAGENT_AUTO_ROUTE_STATUS_PATH = path.join(STATE_DIR, "subagent-auto-route-status.json");
const modes = new Set(["wlb", "official", "supergpt"]);
const safeErrors = new Set(["wlb: 额度查询失败或数据无效", "gpt: 额度查询失败或数据无效"]);
function windowStatus(value, key) {
  if (!["valid", "missing", "invalid"].includes(value?.status)) return { status: "invalid" };
  if (value.status !== "valid") return { status: value.status };
  return typeof value[key] === "number" && Number.isFinite(value[key])
    ? { status: "valid", [key]: value[key] } : { status: "invalid" };
}
function quotaStatus(value, first, key) {
  if (!value) return null;
  return {
    [first]: windowStatus(value[first], key), weekly: windowStatus(value.weekly, key),
    available: typeof value.available === "boolean" ? value.available : null,
  };
}
function safeSnapshot(value) {
  if (!modes.has(value?.route) || typeof value.checkedAt !== "string" || !Number.isFinite(Date.parse(value.checkedAt))) return null;
  return {
    route: value.route, checkedAt: new Date(value.checkedAt).toISOString(),
    degraded: value.degraded === true,
    wlb: quotaStatus(value.wlb, "daily", "remaining"),
    gpt: quotaStatus(value.gpt, "fiveHour", "remainingPercent"),
    errors: Array.isArray(value.errors) ? value.errors.filter((error) => safeErrors.has(error)) : [],
  };
}
function readAutoRouteStatusAt(statusPath, configured) {
  if (configured.route !== "auto") return null;
  try {
    const value = JSON.parse(readFileSync(statusPath, "utf8"));
    return value.version === 1 && value.configuredRevision === configured.revision
      ? safeSnapshot(value.snapshot)
      : null;
  } catch { return null; }
}

async function writeAutoRouteStatusAt(statusPath, routePath, snapshot, configured, readConfigured, { signal } = {}) {
  const safe = safeSnapshot(snapshot);
  if (!safe || configured.route !== "auto") return;
  assertStateOwnership("保存自动路由状态");
  mkdirSync(STATE_DIR, { recursive: true, mode: 0o700 });
  const release = await lockfile.lock(routePath, {
    realpath: false,
    stale: 90000,
    retries: { retries: 20, factor: 1, minTimeout: 100, maxTimeout: 100 },
  });
  const temporary = statusPath + "." + process.pid + "." + randomUUID() + ".tmp";
  try {
    signal?.throwIfAborted();
    const current = readConfigured();
    if (current.route !== "auto" || current.revision !== configured.revision) return;
    if (JSON.stringify(readAutoRouteStatusAt(statusPath, current)) === JSON.stringify(safe)) return;
    writeFileSync(temporary, JSON.stringify({
      version: 1,
      configuredRevision: configured.revision,
      snapshot: safe,
    }) + "\n", { mode: 0o600 });
    await protectPrivateFileAsync(temporary, { signal });
    signal?.throwIfAborted();
    renameSync(temporary, statusPath);
  } finally {
    try {
      try { unlinkSync(temporary); } catch (error) {
        if (error.code !== "ENOENT") throw error;
      }
    } finally {
      await release();
    }
  }
}

export function readAutoRouteStatus(configured = readGptRouteSnapshot()) {
  return readAutoRouteStatusAt(AUTO_ROUTE_STATUS_PATH, configured);
}

export function readSubagentAutoRouteStatus(configured = readSubagentRouteSnapshot()) {
  return readAutoRouteStatusAt(
    SUBAGENT_AUTO_ROUTE_STATUS_PATH,
    configured,
  );
}

export async function writeAutoRouteStatus(snapshot, configured, options = {}) {
  return writeAutoRouteStatusAt(
    AUTO_ROUTE_STATUS_PATH,
    GPT_ROUTE_PATH,
    snapshot,
    configured,
    readGptRouteSnapshot,
    options,
  );
}

export async function writeSubagentAutoRouteStatus(snapshot, configured, options = {}) {
  return writeAutoRouteStatusAt(
    SUBAGENT_AUTO_ROUTE_STATUS_PATH,
    SUBAGENT_ROUTE_PATH,
    snapshot,
    configured,
    readSubagentRouteSnapshot,
    options,
  );
}
