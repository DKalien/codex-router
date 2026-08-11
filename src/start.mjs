import { spawn } from "node:child_process";
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
  SOURCE_ROOT,
  STATE_DIR,
  TARGET,
  loopback,
} from "./paths.mjs";
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
// fetch ignores it unless told. Route native traffic through the local Clash
// mixed port while keeping both domestic providers direct. Override with the
// standard *_PROXY/NO_PROXY variables when needed.
const proxyUrl =
  process.env.HTTPS_PROXY ||
  process.env.https_proxy ||
  "http://127.0.0.1:7897";
const noProxy = [
  process.env.NO_PROXY || process.env.no_proxy || "127.0.0.1,localhost",
  "codex.wlbclub.com",
  "token-plan-cn.xiaomimimo.com",
].join(",");
const commonEnv = {
  NODE_USE_ENV_PROXY: "1",
  HTTP_PROXY: proxyUrl,
  HTTPS_PROXY: proxyUrl,
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

const children = [];
let shuttingDown = false;

function run(command, args, extraEnv = {}) {
  const child = spawn(command, args, {
    cwd: SOURCE_ROOT,
    env: { ...process.env, ...commonEnv, ...extraEnv },
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
  }, 3_000).unref();
}

for (const signal of ["SIGINT", "SIGTERM"]) process.on(signal, stopChildren);

async function main() {
  const router = run(process.execPath, [path.join(SOURCE_ROOT, "src", "router.mjs")]);
  await pollHealth({
    label: "Codex router",
    url: loopback(PORTS.router, "/health"),
    headers: {},
    timeoutMs: 30_000,
    expectedService: "codex-router",
    child: router,
    isShuttingDown: () => shuttingDown,
  });

  console.error(`[codex-router] ready (authenticated loopback endpoint)`);
  const result = await waitForExit(router, "Codex router");
  if (!shuttingDown) {
    console.error(
      `[codex-router] ${result.label} exited (code=${String(result.code)}, signal=${String(result.signal)}).`,
    );
  }
  return result.code || 0;
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
process.exit(exitCode);
