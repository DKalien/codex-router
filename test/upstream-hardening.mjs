import assert from "node:assert/strict";
import { mkdtempSync, rmSync } from "node:fs";
import os from "node:os";
import path from "node:path";
import test from "node:test";

const stateDir = mkdtempSync(path.join(os.tmpdir(), "codex-router-hardening-"));
process.env.MODEL_ROUTER_STATE_DIR = stateDir;

const [{ waitForHealth }, { applyKeepAliveTimeouts, formatErrorChain }, usage] =
  await Promise.all([
    import("../src/health-probe.mjs"),
    import("../src/http-utils.mjs"),
    import("../src/usage-events.mjs"),
  ]);

test.after(() => rmSync(stateDir, { recursive: true, force: true }));

test("health probe drains an unneeded response body", async () => {
  let drained = false;
  await waitForHealth({
    label: "router",
    url: "http://127.0.0.1/health",
    fetchImpl: async () => ({
      ok: true,
      arrayBuffer: async () => {
        drained = true;
        return new ArrayBuffer(0);
      },
    }),
  });
  assert.equal(drained, true);
});

test("keep-alive settings outlast the client pool", async () => {
  const server = {};
  assert.equal(applyKeepAliveTimeouts(server), server);
  assert.equal(server.keepAliveTimeout, 120_000);
  assert.equal(server.headersTimeout, 125_000);
});

test("error cause chains remain bounded and include transport codes", () => {
  const error = new TypeError("fetch failed");
  error.code = "UND_ERR_SOCKET";
  error.cause = Object.assign(new Error("connect reset"), { code: "ECONNRESET" });
  assert.equal(
    formatErrorChain(error),
    "TypeError: fetch failed (UND_ERR_SOCKET) <- Error: connect reset (ECONNRESET)",
  );
  assert.equal(
    formatErrorChain(error, { messages: false }),
    "TypeError (UND_ERR_SOCKET) <- Error (ECONNRESET)",
  );
});

test("usage events preserve the stream-aborted marker", () => {
  usage.recordUsageEvent({
    model: "gpt-5.6-sol",
    provider: "openai",
    status: 502,
    durationMs: 12,
    streamAborted: true,
  });
  const [event] = usage.recentUsageEvents({ sinceMs: 60_000, limit: 1 });
  assert.equal(event.streamAborted, true);
});
