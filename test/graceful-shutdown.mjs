import assert from "node:assert/strict";
import http from "node:http";
import test from "node:test";

import {
  applyKeepAliveTimeouts,
  installGracefulShutdown,
} from "../src/http-utils.mjs";

let signalSequence = 0;

function listen(server) {
  return new Promise((resolve) => {
    server.listen(0, "127.0.0.1", () => resolve(server.address().port));
  });
}

function get(port, path = "/") {
  return new Promise((resolve, reject) => {
    const request = http.get({ host: "127.0.0.1", port, path }, resolve);
    request.on("error", reject);
  });
}

function read(response) {
  let body = "";
  let firstChunk;
  const first = new Promise((resolve) => {
    firstChunk = resolve;
  });
  const done = new Promise((resolve, reject) => {
    response.setEncoding("utf8");
    response.on("data", (chunk) => {
      body += chunk;
      firstChunk();
    });
    response.once("end", () => resolve(body));
    response.once("error", reject);
  });
  return { first, done };
}

function shutdownHarness(server, drainMs) {
  const signal = `codex-router-test-shutdown-${++signalSequence}`;
  let resolveExit;
  const exited = new Promise((resolve) => {
    resolveExit = resolve;
  });
  installGracefulShutdown(server, {
    label: "test",
    signals: [signal],
    drainMs,
    exit: resolveExit,
  });
  return { signal, exited };
}

async function close(server) {
  if (server.listening) await new Promise((resolve) => server.close(resolve));
}

test("流式响应在 shutdown 时 clean EOF 并发送 terminal SSE error", async () => {
  const server = http.createServer((_request, response) => {
    response.writeHead(200, { "Content-Type": "text/event-stream" });
    response.write("data: first\n\n");
  });
  applyKeepAliveTimeouts(server);
  const port = await listen(server);
  const { signal, exited } = shutdownHarness(server, 25);

  try {
    const response = await get(port, "/stream");
    const stream = read(response);
    await stream.first;
    process.emit(signal);
    const body = await stream.done;
    const frame = JSON.parse(body.match(/event: error\ndata: (\{.*\})\n\n/)?.[1] || "null");
    assert.equal(response.complete, true);
    assert.match(body, /^data: first\n\n/);
    assert.equal(frame.code, "local_router_stream_failed");
    assert.match(frame.message, /restarting/);
    assert.equal(await exited, 0);
  } finally {
    process.removeAllListeners(signal);
    await close(server);
  }
});

test("未发送 response head 的请求在 shutdown 时返回 503", async () => {
  let resolveArrived;
  const arrived = new Promise((resolve) => {
    resolveArrived = resolve;
  });
  const server = http.createServer(() => resolveArrived());
  applyKeepAliveTimeouts(server);
  const port = await listen(server);
  const { signal, exited } = shutdownHarness(server, 25);

  try {
    const pending = get(port, "/responses");
    await arrived;
    process.emit(signal);
    const response = await pending;
    const { done } = read(response);
    const body = await done;
    assert.equal(response.statusCode, 503);
    assert.equal(JSON.parse(body).error.type, "local_router_restarting");
    assert.equal(await exited, 0);
  } finally {
    process.removeAllListeners(signal);
    await close(server);
  }
});

test("空闲 keep-alive 服务不等待 drain window 即退出", async () => {
  const server = http.createServer((_request, response) => response.end("ok"));
  applyKeepAliveTimeouts(server);
  const port = await listen(server);
  const { signal, exited } = shutdownHarness(server, 30_000);

  try {
    const response = await get(port, "/health");
    await read(response).done;
    const startedAt = Date.now();
    process.emit(signal);
    assert.equal(await exited, 0);
    assert.ok(Date.now() - startedAt < 5_000);
  } finally {
    process.removeAllListeners(signal);
    await close(server);
  }
});
