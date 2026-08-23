import { Readable, Transform } from "node:stream";
import { pipeline } from "node:stream/promises";

import { secretEqual } from "./caller-auth.mjs";
import { TARGET } from "./paths.mjs";

export const MAX_BODY_BYTES = Number(
  process.env.MODEL_ROUTER_MAX_BODY_BYTES ||
    (TARGET === "codex"
      ? process.env.CODEX_ROUTER_MAX_BODY_BYTES || process.env.KIMI_PROXY_MAX_BODY_BYTES
      : undefined) ||
    64 * 1024 * 1024,
);

export const HOP_BY_HOP_HEADERS = new Set([
  "connection",
  "content-encoding",
  "content-length",
  "host",
  "keep-alive",
  "proxy-authenticate",
  "proxy-authorization",
  "set-cookie",
  "te",
  "trailer",
  "transfer-encoding",
  "upgrade",
]);

export const KEEPALIVE_TIMEOUT_MS = 120_000;
export const HEADERS_TIMEOUT_MS = KEEPALIVE_TIMEOUT_MS + 5_000;

export function applyKeepAliveTimeouts(server) {
  server.keepAliveTimeout = KEEPALIVE_TIMEOUT_MS;
  server.headersTimeout = HEADERS_TIMEOUT_MS;
  return server;
}

export const SHUTDOWN_DRAIN_MS = (() => {
  const configured = Number(process.env.MODEL_ROUTER_SHUTDOWN_DRAIN_MS || 2_000);
  return Number.isFinite(configured) && configured >= 0 ? configured : 2_000;
})();
export const SHUTDOWN_FLUSH_MS = 1_000;
const SHUTDOWN_MESSAGE = "The local router is restarting; retry the request.";

export function installGracefulShutdown(
  server,
  {
    label = "codex-router",
    signals = ["SIGINT", "SIGTERM"],
    drainMs = SHUTDOWN_DRAIN_MS,
    flushMs = SHUTDOWN_FLUSH_MS,
    exit = (code) => process.exit(code),
  } = {},
) {
  const live = new Set();
  server.on("request", (_request, response) => {
    live.add(response);
    response.once("close", () => live.delete(response));
  });

  let shuttingDown = false;
  let exited = false;
  let drainTimer;
  let flushTimer;
  const finish = () => {
    if (exited) return;
    exited = true;
    if (drainTimer) clearTimeout(drainTimer);
    if (flushTimer) clearTimeout(flushTimer);
    exit(0);
  };

  const shutdown = () => {
    if (shuttingDown) return;
    shuttingDown = true;
    server.close(finish);
    server.closeIdleConnections?.();
    if (exited) return;
    drainTimer = setTimeout(() => {
      for (const response of live) {
        if (response.writableEnded || response.destroyed) continue;
        try {
          if (response.headersSent) {
            endStreamedResponse(response, {
              message: SHUTDOWN_MESSAGE,
            });
          } else {
            writeJson(response, 503, {
              error: {
                type: "local_router_restarting",
                message: SHUTDOWN_MESSAGE,
              },
            });
          }
        } catch {
          // The client may close the socket while the drain timer is firing.
        }
      }
      if (exited) return;
      flushTimer = setTimeout(() => {
        server.closeAllConnections?.();
        finish();
      }, flushMs);
      flushTimer.unref?.();
    }, drainMs);
    drainTimer.unref?.();
    if (live.size > 0) {
      console.error(
        `[${label}] shutting down with ${live.size} request(s) in flight; draining for up to ${drainMs}ms`,
      );
    }
  };

  for (const signal of signals) process.on(signal, shutdown);
  return server;
}

export const MAX_UPSTREAM_ERROR_BYTES = 64 * 1024;
export const DEFAULT_STREAM_IDLE_TIMEOUT_MS = 300_000;
export const MIN_STREAM_IDLE_TIMEOUT_MS = 10;
export const MAX_STREAM_IDLE_TIMEOUT_MS = 15 * 60_000;

function boundedMilliseconds(value, fallback) {
  const parsed = Number(value);
  return Number.isFinite(parsed) && parsed > 0
    ? Math.min(MAX_STREAM_IDLE_TIMEOUT_MS, Math.max(MIN_STREAM_IDLE_TIMEOUT_MS, Math.floor(parsed)))
    : fallback;
}

export function streamIdleTimeoutMs(value) {
  if (value !== undefined) return boundedMilliseconds(value, DEFAULT_STREAM_IDLE_TIMEOUT_MS);
  return boundedMilliseconds(
    process.env.CODEX_ROUTER_STREAM_IDLE_TIMEOUT_MS ||
      process.env.MODEL_ROUTER_STREAM_IDLE_TIMEOUT_MS,
    DEFAULT_STREAM_IDLE_TIMEOUT_MS,
  );
}

export async function readResponseTextLimited(response, maxBytes = MAX_UPSTREAM_ERROR_BYTES) {
  if (!response?.body) return "";
  const limit = Math.max(1, Math.floor(maxBytes));
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  const chunks = [];
  let total = 0;
  try {
    while (total < limit) {
      const { done, value } = await reader.read();
      if (done) break;
      const chunk = Buffer.from(value);
      const remaining = limit - total;
      const selected = chunk.byteLength > remaining ? chunk.subarray(0, remaining) : chunk;
      chunks.push(selected);
      total += selected.byteLength;
      if (selected.byteLength < chunk.byteLength || total >= limit) {
        try {
          await reader.cancel("upstream error body limit reached");
        } catch {
          // The upstream may already have closed its body.
        }
        break;
      }
    }
    return decoder.decode(Buffer.concat(chunks));
  } finally {
    reader.releaseLock();
  }
}

export async function readRequestBody(request) {
  const chunks = [];
  let total = 0;
  for await (const chunk of request) {
    total += chunk.length;
    if (total > MAX_BODY_BYTES) {
      const error = new Error(`Request body exceeds ${MAX_BODY_BYTES} bytes.`);
      error.status = 413;
      throw error;
    }
    chunks.push(chunk);
  }
  return Buffer.concat(chunks);
}

export function writeJson(response, status, payload) {
  const body = Buffer.from(JSON.stringify(payload), "utf8");
  response.writeHead(status, {
    "Content-Type": "application/json",
    "Content-Length": String(body.length),
  });
  response.end(body);
}

const MAX_ERROR_CHAIN_DEPTH = 8;

export function formatErrorChain(error, { messages = true } = {}) {
  const parts = [];
  let current = error;
  for (let depth = 0; current != null && depth < MAX_ERROR_CHAIN_DEPTH; depth += 1) {
    if (typeof current !== "object") {
      parts.push(String(current));
      break;
    }
    const name = typeof current.name === "string" && current.name ? current.name : "Error";
    const message =
      messages && typeof current.message === "string" && current.message
        ? `: ${current.message}`
        : "";
    const code = current.code === undefined ? "" : ` (${String(current.code)})`;
    parts.push(`${name}${message}${code}`);
    current = current.cause ?? (Array.isArray(current.errors) ? current.errors[0] : undefined);
  }
  return parts.length ? parts.join(" <- ") : String(error);
}

export function httpErrorStatus(error, fallback = 502) {
  const status = Number(error?.status);
  return Number.isInteger(status) && status >= 400 && status <= 599
    ? status
    : fallback;
}

export function copyResponseHeaders(upstream, response, denylist = HOP_BY_HOP_HEADERS) {
  for (const [name, value] of upstream.headers.entries()) {
    if (!denylist.has(name.toLowerCase())) response.setHeader(name, value);
  }
}

function isEventStream(response) {
  return String(response.getHeader("content-type") || "")
    .toLowerCase()
    .includes("text/event-stream");
}

function finishResponse(response) {
  return new Promise((resolve) => {
    if (response.writableFinished || response.destroyed) {
      resolve();
      return;
    }
    response.once("finish", resolve);
    // A client that hangs up before the last chunk drains emits "close"
    // without "finish"; the request is over either way.
    response.once("close", resolve);
    if (!response.writableEnded) response.end();
  });
}

function idleTimeoutTransform(timeoutMs) {
  let timer;
  const guard = new Transform({
    transform(chunk, _encoding, callback) {
      if (timer) clearTimeout(timer);
      timer = setTimeout(() => {
        const error = new Error("Upstream response stream idle timeout.");
        error.code = "ERR_ROUTER_STREAM_IDLE_TIMEOUT";
        guard.destroy(error);
      }, timeoutMs);
      timer.unref?.();
      callback(null, chunk);
    },
    flush(callback) {
      if (timer) clearTimeout(timer);
      timer = undefined;
      callback();
    },
  });
  timer = setTimeout(() => {
    const error = new Error("Upstream response stream idle timeout.");
    error.code = "ERR_ROUTER_STREAM_IDLE_TIMEOUT";
    guard.destroy(error);
  }, timeoutMs);
  timer.unref?.();
  guard.once("close", () => {
    if (timer) clearTimeout(timer);
    timer = undefined;
  });
  return guard;
}

// Terminate a response whose body is already streaming.
//
// `response.destroy()` resets the socket, so an in-flight chunked body loses
// its terminating `0\r\n\r\n` and the client reports a transport failure
// ("error decoding response body") with nothing to say about the cause. Ending
// the body instead produces a well-formed, if short, HTTP message.
//
// A gracefully ended SSE stream, though, is indistinguishable from a completed
// one: the turn would simply look short and successful. So on `text/event-stream`
// we first emit a terminal `error` event, matching the event framing the router
// already writes elsewhere and the Responses API's own `error` event. A parser
// that understands it surfaces a real failure; one that does not ignores the
// unknown event and lands on the plain graceful end, which is still strictly
// better than a reset. The frame carries a fixed router-side message and never
// upstream error text, so no response body can leak through it.
//
// The frame is prefixed with a blank line because the stream is being ended at
// the point upstream died, which is very often mid-line: transforms forward
// upstream's chunk boundaries verbatim, and a single `output_text.delta` can
// carry a long span. Writing `event: error` straight onto an unterminated
// `data:` line does not produce an error event at all -- a conforming parser
// reads the field name as more of the previous event's data, so the failure
// signal turns into garbage appended to the last delta, which is exactly the
// silent corruption this function exists to avoid. Leading newlines are inert
// when the stream did end cleanly: a blank line with no buffered fields
// dispatches nothing.
export function endStreamedResponse(response, forceEventStream = false) {
  if (!response || response.writableEnded || response.destroyed) return;
  const options =
    forceEventStream && typeof forceEventStream === "object"
      ? { forceEventStream: true, ...forceEventStream }
      : { forceEventStream };
  if (options.forceEventStream || isEventStream(response)) {
    try {
      const data = {
        type: "error",
        code: "local_router_stream_failed",
        message: options.message || "The local router lost the upstream response stream.",
        param: null,
      };
      response.write(`\n\nevent: error\ndata: ${JSON.stringify(data)}\n\n`);
    } catch {
      // The socket may already be gone; ending below is still correct.
    }
  }
  response.end();
}

export async function pipeResponse(upstream, response, denylist, transform, options = {}) {
  const transforms = transform === undefined
    ? []
    : Array.isArray(transform)
      ? transform
      : [transform];
  response.statusCode = upstream.status;
  copyResponseHeaders(upstream, response, denylist);
  if (!upstream.body) {
    response.end();
    return;
  }
  const source = Readable.fromWeb(upstream.body);
  const idleGuard = idleTimeoutTransform(streamIdleTimeoutMs(options.idleTimeoutMs));
  try {
    // `pipeline` forwards errors and destroys every stream in the chain, which
    // `.pipe()` does not: a mid-stream upstream failure used to leave the
    // response half-written and open forever. `end: false` keeps the response
    // itself out of that teardown so the caller can end the body cleanly (see
    // `endStreamedResponse`) instead of resetting the socket.
    await pipeline(source, idleGuard, ...transforms, response, { end: false });
  } catch (error) {
    if (error?.code === "ERR_ROUTER_STREAM_IDLE_TIMEOUT" && !response.destroyed) {
      const eventStream = options.eventStream === true || isEventStream(response);
      if (eventStream) {
        if (!response.headersSent && !response.hasHeader("content-type")) {
          response.setHeader("Content-Type", "text/event-stream");
        }
        if (!response.headersSent) response.flushHeaders?.();
        endStreamedResponse(response, true);
      } else if (!response.headersSent) {
        writeJson(response, 504, {
          error: {
            type: "local_router_stream_failed",
            message: "The upstream response stream stopped producing data.",
          },
        });
      } else {
        response.end();
      }
    }
    // A client that disconnects mid-stream destroys the response, which
    // pipeline reports as a premature close. That is not a router failure, and
    // pipeline has already torn the upstream read down, so the in-flight
    // counter releases without inventing an error.
    if (response.destroyed && !response.writableFinished) return;
    throw error;
  }
  await finishResponse(response);
}

export function requireInternalAuth(request, response, secret) {
  const authorized = secretEqual(
    request.headers.authorization,
    `Bearer ${secret}`,
  ) || secretEqual(request.headers["x-api-key"], secret);
  if (!authorized) {
    writeJson(response, 401, {
      error: {
        type: "authentication_error",
        message: "This internal loopback route requires the router service key.",
      },
    });
  }
  return authorized;
}
