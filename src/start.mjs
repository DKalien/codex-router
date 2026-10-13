import { spawn } from "node:child_process";
import net from "node:net";
import {
  existsSync,
  mkdirSync,
  readFileSync,
  renameSync,
  unlinkSync,
  writeFileSync,
} from "node:fs";
import path from "node:path";

import { assertCallerSecret } from "./caller-auth.mjs";
import {
  CALLER_SECRET_PATH,
  PORTS,
  SHUTDOWN_REQUEST_PATH,
  SOURCE_ROOT,
  STATE_DIR,
  TARGET,
  loopback,
} from "./paths.mjs";
import { SHUTDOWN_DRAIN_MS, SHUTDOWN_FLUSH_MS } from "./http-utils.mjs";
import { waitForHealth as pollHealth } from "./health-probe.mjs";

// Lite: the router is the whole service. Routed providers speak the Responses
// API natively, so there is no LiteLLM gateway or credential forwarder to
// supervise -- one child process is the entire pipeline.
if (!existsSync(CALLER_SECRET_PATH)) {
  throw new Error(`Router caller key is missing; run ./bin/install.`);
}
const callerKey = assertCallerSecret(
  readFileSync(CALLER_SECRET_PATH, "utf8").trim(),
);

const servicePidPath =
  process.env.CODEX_ROUTER_SERVICE_PID_PATH ||
  (process.platform === "win32" && process.env.CODEX_ROUTER_QUIET === "1"
    ? path.join(STATE_DIR, "service.pid")
    : "");

function writeServicePid() {
  if (!servicePidPath) return;
  mkdirSync(path.dirname(servicePidPath), { recursive: true });
  const value = `${process.pid}\n`;
  const temporary = `${servicePidPath}.tmp.${process.pid}`;
  try {
    writeFileSync(temporary, value);
    renameSync(temporary, servicePidPath);
  } finally {
    try {
      if (existsSync(temporary)) unlinkSync(temporary);
    } catch {
      // A failed cleanup must not hide the original write error.
    }
  }
  process.on("exit", () => {
    try {
      if (readFileSync(servicePidPath, "utf8") === value) unlinkSync(servicePidPath);
    } catch {
      // A replacement service owns a different PID, or the manager removed it.
    }
  });
}

writeServicePid();

// Native GPT traffic is proxied to chatgpt.com, which is unreachable from
// a direct connection here; Codex itself rides the system proxy, but Node's
// fetch ignores it unless told. Route native traffic through the local proxy
// tool while keeping both domestic providers direct via NO_PROXY below -- the
// routed providers never touch the proxy, so they keep working when it is
// offline. The proxy port is taken from the standard *_PROXY variables when
// set; otherwise the first listening local port wins, v2rayN's mixed/HTTP
// ports ahead of the legacy Clash ones. With no listener at all the native
// route runs direct and fails on its own, instead of poisoning every route
// with instant ECONNREFUSED.
// Overridable via CODEX_ROUTER_PROXY_PORTS="10808,7897" for exotic proxy
// tools and for isolated watchdog testing.
const PROXY_CANDIDATE_PORTS = (
  process.env.CODEX_ROUTER_PROXY_PORTS || "10808,10809,7897,7890"
)
  .split(",")
  .map((value) => Number(value.trim()))
  .filter((value) => Number.isInteger(value) && value >= 1 && value <= 65_535);

function localPortListening(port) {
  return new Promise((resolve) => {
    const socket = net.connect({ host: "127.0.0.1", port });
    const finish = (listening) => {
      socket.destroy();
      resolve(listening);
    };
    socket.setTimeout(500, () => finish(false));
    socket.once("connect", () => finish(true));
    socket.once("error", () => finish(false));
  });
}

async function resolveUpstreamProxy() {
  const override = (
    process.env.HTTPS_PROXY ||
    process.env.https_proxy ||
    process.env.HTTP_PROXY ||
    process.env.http_proxy ||
    ""
  ).trim();
  if (override) return { url: override, source: "environment" };
  for (const port of PROXY_CANDIDATE_PORTS) {
    if (await localPortListening(port)) {
      return { url: `http://127.0.0.1:${port}`, source: "detected" };
    }
  }
  return { url: "", source: "none" };
}

const { url: proxyUrl, source: proxySource } = await resolveUpstreamProxy();
console.error(
  proxyUrl
    ? `[codex-router] native upstream proxy: ${proxyUrl} (${proxySource})`
    : "[codex-router] no local proxy detected; native upstream will run direct",
);
const noProxy = [
  process.env.NO_PROXY || process.env.no_proxy || "127.0.0.1,localhost",
  "codex.wlbclub.com",
  "token-plan-cn.xiaomimimo.com",
].join(",");
function buildCommonEnv(upstreamProxyUrl) {
  const commonEnv = {
    NO_PROXY: noProxy,
    MODEL_ROUTER_TARGET: TARGET,
    MODEL_ROUTER_STATE_DIR: STATE_DIR,
    MODEL_ROUTER_CALLER_KEY: callerKey,
    MODEL_ROUTER_PORT: String(PORTS.router),
    MODEL_ROUTER_QUIET: "1",
    CODEX_ROUTER_CALLER_KEY: callerKey,
    CODEX_ROUTER_PORT: String(PORTS.router),
    NO_COLOR: "1",
  };
  if (upstreamProxyUrl) {
    commonEnv.NODE_USE_ENV_PROXY = "1";
    commonEnv.HTTP_PROXY = upstreamProxyUrl;
    commonEnv.HTTPS_PROXY = upstreamProxyUrl;
  }
  return commonEnv;
}

const children = [];
let shuttingDown = false;
const SIGKILL_AFTER_MS = SHUTDOWN_DRAIN_MS + SHUTDOWN_FLUSH_MS + 2_000;

function run(command, args, upstreamProxyUrl, extraEnv = {}) {
  const child = spawn(command, args, {
    cwd: SOURCE_ROOT,
    env: { ...process.env, ...buildCommonEnv(upstreamProxyUrl), ...extraEnv },
    stdio: "inherit",
  });
  children.push(child);
  return child;
}

function waitForExit(child, label) {
  if (child.exitCode !== null || child.signalCode !== null) {
    return Promise.resolve({ label, code: child.exitCode, signal: child.signalCode });
  }
  return new Promise((resolve) => {
    child.once("exit", (code, signal) => resolve({ label, code, signal }));
  });
}

function stopChildren() {
  if (shuttingDown) return;
  shuttingDown = true;
  for (const child of children) {
    if (child.exitCode === null && child.signalCode === null) child.kill("SIGTERM");
  }
  setTimeout(() => {
    for (const child of children) {
      if (child.exitCode === null && child.signalCode === null) child.kill("SIGKILL");
    }
  }, SIGKILL_AFTER_MS).unref();
}

for (const signal of ["SIGINT", "SIGTERM"]) process.on(signal, stopChildren);

function sleep(ms) {
  return new Promise((resolve) => {
    // Unref'd: the spawned child handle keeps the supervisor alive; a pending
    // watchdog nap must never hold the process open on its own.
    const timer = setTimeout(resolve, ms);
    timer.unref?.();
  });
}

// Windows has no cross-process SIGTERM, so the graceful path is the
// shutdown.request file the router child watches. Write it first, then send
// SIGTERM as the belt for POSIX and as the immediate stop when the file watch
// is somehow not armed.
function requestRouterChildShutdown(router) {
  try {
    writeFileSync(SHUTDOWN_REQUEST_PATH, `${Date.now()}\n`);
  } catch {
    // The SIGTERM below remains; the child also clears stale files on boot.
  }
  router.kill("SIGTERM");
}

// Watchdog while a child runs: report a confirmed live proxy that differs
// from the one the child was launched with. "Proxy vanished" deliberately
// never restarts -- a dead-proxy env and a no-proxy env fail the native route
// identically, and a respawn would only cut healthy domestic streams. This
// covers the common boot order (router first, the proxy tool switched on
// later) and a tool reappearing on a different candidate port.
const PROXY_WATCHDOG_POLL_MS = 12_000;
const PROXY_WATCHDOG_STABLE_MS = 2_500;

async function waitForProxyChange(router, currentProxyUrl) {
  for (;;) {
    await sleep(PROXY_WATCHDOG_POLL_MS);
    if (shuttingDown || router.exitCode !== null || router.signalCode !== null) return null;
    const next = await resolveUpstreamProxy();
    if (!next.url || next.url === currentProxyUrl) continue;
    // Require the new state to hold across two probes, so a tool merely
    // restarting on its own port does not flap the child.
    await sleep(PROXY_WATCHDOG_STABLE_MS);
    if (shuttingDown || router.exitCode !== null || router.signalCode !== null) return null;
    const confirmed = await resolveUpstreamProxy();
    if (confirmed.url && confirmed.url === next.url) return confirmed.url;
  }
}

async function main() {
  let currentProxyUrl = proxyUrl;
  let booted = false;
  for (;;) {
    const router = run(
      process.execPath,
      [path.join(SOURCE_ROOT, "src", "router.mjs")],
      currentProxyUrl,
    );
    await pollHealth({
      label: "Codex router",
      url: loopback(PORTS.router, "/health"),
      headers: {},
      timeoutMs: 30_000,
      expectedService: "codex-router",
      child: router,
      isShuttingDown: () => shuttingDown,
    });
    if (booted) {
      console.error(
        `[codex-router] router child restarted with upstream proxy ${currentProxyUrl || "none"}`,
      );
    } else {
      booted = true;
      console.error(`[codex-router] ready (authenticated loopback endpoint)`);
    }

    const exitPromise = waitForExit(router, "Codex router");
    const changePromise =
      proxySource === "environment"
        ? new Promise(() => {}) // an explicit *_PROXY override is authoritative
        : waitForProxyChange(router, currentProxyUrl);
    const outcome = await Promise.race([
      exitPromise.then((result) => ({ kind: "exit", result })),
      changePromise.then((url) => ({ kind: "proxy-change", url })),
    ]);

    if (outcome.kind === "exit") {
      if (!shuttingDown) {
        console.error(
          `[codex-router] ${outcome.result.label} exited (code=${String(outcome.result.code)}, signal=${String(outcome.result.signal)}).`,
        );
      }
      return outcome.result.code || 0;
    }

    console.error(
      `[codex-router] upstream proxy now ${outcome.url} (was ${currentProxyUrl || "none"}); restarting router child`,
    );
    requestRouterChildShutdown(router);
    const forced = await Promise.race([
      exitPromise.then(() => false),
      sleep(SIGKILL_AFTER_MS).then(() => true),
    ]);
    if (forced) router.kill("SIGKILL");
    await exitPromise;
    currentProxyUrl = outcome.url;
  }
}

let exitCode = 0;
try {
  exitCode = await main();
} catch (error) {
  if (!shuttingDown) {
    const reason = (error instanceof Error && error.message) || String(error);
    console.error(`[model-router] startup failed: ${reason}; inspect the service logs above for details.`);
    exitCode = 1;
  }
} finally {
  stopChildren();
  await Promise.all(children.map((child) => waitForExit(child, "child")));
}
process.exitCode = exitCode;
