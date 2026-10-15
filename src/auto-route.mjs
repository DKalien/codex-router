import { createHash } from "node:crypto";
import { MAX_UPSTREAM_ERROR_BYTES, readResponseTextLimited } from "./http-utils.mjs";

const ROUTES = ["wlb", "official", "supergpt"];
const object = (v) => v !== null && typeof v === "object" && !Array.isArray(v);
const numeric = (v) => (typeof v === "number" || (typeof v === "string" && v.trim() !== "")) && Number.isFinite(Number(v));
const missing = () => ({ status: "missing" });
const invalid = () => ({ status: "invalid" });
const unwrap = (v) => object(v?.data) ? v.data : v;

function wlbWindow(entry) {
  if (entry === undefined) return missing();
  if (!object(entry)) return invalid();
  let remaining;
  if (entry.remaining !== undefined) {
    if (!numeric(entry.remaining)) return invalid();
    remaining = Number(entry.remaining);
  } else if (numeric(entry.limit) && numeric(entry.used) && Number(entry.limit) >= 0 && Number(entry.used) >= 0) {
    remaining = Number(entry.limit) - Number(entry.used);
  } else return invalid();
  return { status: "valid", remaining };
}

export function parseWlbUsage(value) {
  const limits = unwrap(value)?.rate_limits;
  const find = (name) => Array.isArray(limits)
    ? limits.find((entry) => object(entry) && entry.window === name)
    : object(limits) ? limits[name] : undefined;
  const daily = wlbWindow(find("1d"));
  const weekly = wlbWindow(find("7d"));
  const windows = [daily, weekly];
  const available = windows.some((w) => w.status === "valid" && w.remaining <= 0)
    ? false : windows.some((w) => w.status === "invalid") || weekly.status !== "valid" ? null : true;
  return { daily, weekly, available };
}

function gptWindow(entry) {
  if (entry == null) return missing();
  if (!object(entry)) return invalid();
  const percent = entry.used_percent !== undefined ? entry.used_percent : entry.usedPercent;
  if (!numeric(percent) || Number(percent) < 0 || Number(percent) > 100) return invalid();
  return { status: "valid", remainingPercent: 100 - Number(percent) };
}

export function parseGptUsage(value) {
  const source = unwrap(value);
  let primary;
  let secondary;
  const limits = source?.rate_limits;
  if (Array.isArray(limits)) {
    for (const entry of limits) {
      if (!object(entry)) continue;
      const label = String(entry.window || entry.type || "").toLowerCase();
      const seconds = Number(entry.limit_window_seconds ?? Number(entry.window_minutes) * 60);
      if (seconds === 604800 || ["weekly", "7d", "secondary"].includes(label)) secondary ??= entry;
      else if (seconds === 18000 || ["primary", "5h", "5_hour", "5-hour"].includes(label)) primary ??= entry;
    }
  } else if (object(limits)) {
    primary = limits.primary !== undefined ? limits.primary : limits.primary_window;
    secondary = limits.secondary !== undefined ? limits.secondary : limits.secondary_window;
  }
  if (primary === undefined) primary = source?.rate_limit?.primary_window;
  if (secondary === undefined) secondary = source?.rate_limit?.secondary_window;
  if (object(primary) && Number(primary.limit_window_seconds ?? Number(primary.window_minutes) * 60) === 604800) {
    if (secondary == null) secondary = primary;
    primary = undefined;
  }
  const fiveHour = gptWindow(primary);
  const weekly = gptWindow(secondary);
  const lowFiveHour = fiveHour.status === "valid" && fiveHour.remainingPercent <= 5;
  const lowWeekly = weekly.status === "valid" && weekly.remainingPercent <= 2;
  const invalidWindow = fiveHour.status === "invalid" || weekly.status === "invalid";
  const available = lowFiveHour || lowWeekly
    ? false
    : invalidWindow || (fiveHour.status === "missing" && weekly.status === "missing")
      ? null : true;
  return { fiveHour, weekly, available };
}

function unavailable() {
  return Object.assign(new Error("自动路由暂时无法可靠判定可用额度，请稍后重试。"), { status: 503, type: "auto_route_unavailable", code: "auto_route_unavailable" });
}
function abortError() {
  return Object.assign(new Error("自动路由请求已取消。"), { name: "AbortError" });
}
function waitFor(promise, signal) {
  if (!signal) return promise;
  if (signal.aborted) return Promise.reject(abortError());
  return new Promise((resolve, reject) => {
    const onAbort = () => reject(abortError());
    signal.addEventListener("abort", onAbort, { once: true });
    promise.then(resolve, reject).finally(() => signal.removeEventListener("abort", onAbort));
  });
}

export function createAutoRouteController({
  getWlbConnection,
  officialUsageUrl = "https://chatgpt.com/backend-api/wham/usage",
  ttlMs = 30000,
  timeoutMs = 5000,
  now = Date.now,
  fetchImpl = globalThis.fetch,
}) {
  const cache = new Map();
  async function query(url, headers, parser) {
    const timeout = new AbortController();
    const timer = setTimeout(() => timeout.abort(), timeoutMs);
    try {
      const endpoint = new URL(url);
      if (!["http:", "https:"].includes(endpoint.protocol) || endpoint.username || endpoint.password) throw unavailable();
      const response = await fetchImpl(endpoint.href, { method: "GET", headers, redirect: "error", signal: timeout.signal });
      if (!response.ok) {
        void response.body?.cancel().catch(() => {});
        throw unavailable();
      }
      const text = await readResponseTextLimited(response, MAX_UPSTREAM_ERROR_BYTES, { signal: timeout.signal, idleTimeoutMs: timeoutMs });
      if (Buffer.byteLength(text) >= MAX_UPSTREAM_ERROR_BYTES) throw unavailable();
      return parser(JSON.parse(text));
    } finally { clearTimeout(timer); }
  }
  async function refresh(entry, headers) {
    const exclusions = new Map(entry.excluded);
    const results = await Promise.allSettled([
      (async () => {
        const { baseUrl, credential } = getWlbConnection();
        if (typeof credential !== "string" || !credential || typeof baseUrl !== "string" || !baseUrl.trim()) throw unavailable();
        let base = baseUrl.trim().replace(/\/+$/, "");
        if (!/\/v1\/usage$/i.test(base)) base += /\/v1$/i.test(base) ? "/usage" : "/v1/usage";
        return query(base, { authorization: `Bearer ${credential}` }, parseWlbUsage);
      })(),
      (async () => {
        if (typeof headers.authorization !== "string" || !headers.authorization.trim()) throw unavailable();
        const identity = { authorization: headers.authorization, "User-Agent": "Mozilla/5.0 Codex-Usage/1.0", Originator: "codex-cli" };
        if (typeof headers["chatgpt-account-id"] === "string") identity["chatgpt-account-id"] = headers["chatgpt-account-id"];
        return query(officialUsageUrl, identity, parseGptUsage);
      })(),
    ]);
    entry.errors = [];
    for (const [index, name] of ["wlb", "gpt"].entries()) {
      const result = results[index];
      const route = index === 0 ? "wlb" : "official";
      entry[name] = result.status === "fulfilled" ? result.value : null;
      if (result.status !== "fulfilled" || result.value.available === null) entry.errors.push(`${name}: 额度查询失败或数据无效`);
      else if (entry.excluded.get(route) === exclusions.get(route)) entry.excluded.delete(route);
    }
    if (results.some((result) => result.status === "fulfilled" && result.value.available !== null)
      && entry.excluded.get("supergpt") === exclusions.get("supergpt")) entry.excluded.delete("supergpt");
    entry.checkedAt = new Date(now()).toISOString();
    entry.updatedAt = now();
  }
  return {
    async resolve(headers, { signal, exclude = [] } = {}) {
      if (signal?.aborted) throw abortError();
      const key = createHash("sha256").update(JSON.stringify([headers.authorization, headers["chatgpt-account-id"]])).digest("hex");
      let entry = cache.get(key);
      if (!entry) {
        entry = { excluded: new Map(), sequence: 0, updatedAt: -Infinity, route: null, wlb: null, gpt: null };
        if (cache.size >= 16) cache.delete(cache.keys().next().value);
      }
      cache.delete(key);
      cache.set(key, entry);
      // 刚报告的额度失败必须覆盖本轮可能滞后的查询结果。
      const newExclusions = exclude.filter((route) => ROUTES.includes(route));
      if (now() - entry.updatedAt >= ttlMs) {
        entry.pending ??= refresh(entry, headers).finally(() => { entry.pending = null; });
      }
      for (const route of newExclusions) entry.excluded.set(route, ++entry.sequence);
      if (entry.pending) await waitFor(entry.pending, signal);
      if (signal?.aborted) throw abortError();
      const states = { wlb: entry.wlb?.available ?? null, official: entry.gpt?.available ?? null, supergpt: true };
      for (const route of entry.excluded.keys()) states[route] = false;
      let route;
      for (const candidate of ROUTES) {
        if (states[candidate] === false) continue;
        if (states[candidate] === true) route = candidate;
        break;
      }
      if (!route && entry.route && states[entry.route] !== false) route = entry.route;
      if (!route) throw unavailable();
      entry.route = route;
      return { route, checkedAt: entry.checkedAt, wlb: entry.wlb, gpt: entry.gpt, degraded: entry.errors.length > 0, errors: [...entry.errors] };
    },
  };
}
