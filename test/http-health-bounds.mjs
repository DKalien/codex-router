import assert from "node:assert/strict";
import { Readable } from "node:stream";
import test from "node:test";

import { readRequestBody, readResponseBody } from "../src/http-utils.mjs";
import { waitForRouterHealth } from "../src/router-health.mjs";

test("upstream response 超限时立即取消余流", async () => {
  let canceled = false;
  let pulls = 0;
  const body = new ReadableStream({
    pull(controller) {
      pulls += 1;
      controller.enqueue(new TextEncoder().encode(pulls === 1 ? "1234" : "5678"));
    },
    cancel() {
      canceled = true;
    },
  });

  await assert.rejects(
    readResponseBody(new Response(body), { maxBytes: 4 }),
    (error) => {
      assert.equal(error.status, 502);
      assert.equal(error.code, "ERR_UPSTREAM_RESPONSE_TOO_LARGE");
      return true;
    },
  );
  assert.equal(canceled, true);
  assert.equal(pulls, 2);
});

test("request 超限后继续 drain，完成后再返回 413", async () => {
  const request = Readable.from([
    Buffer.from("1234"),
    Buffer.from("5678"),
    Buffer.from("tail"),
  ]);

  await assert.rejects(readRequestBody(request, { maxBytes: 4 }), (error) => {
    assert.equal(error.status, 413);
    assert.match(error.message, /4 bytes/);
    return true;
  });
  assert.equal(request.readableEnded, true);
});

test("request drain 可被客户端中止", async () => {
  let pushed = false;
  const request = new Readable({
    read() {
      if (!pushed) {
        pushed = true;
        this.push(Buffer.from("12345"));
      }
    },
  });
  const controller = new AbortController();
  const reason = new Error("client closed");
  const reading = readRequestBody(request, { maxBytes: 4, signal: controller.signal });
  setImmediate(() => controller.abort(reason));

  await assert.rejects(reading, reason);
});

test("upstream response read 可被请求 signal 中止", async () => {
  let canceled = false;
  const body = new ReadableStream({
    pull() {
      return new Promise(() => {});
    },
    cancel() {
      canceled = true;
    },
  });
  const controller = new AbortController();
  const reason = new Error("request ended");
  const reading = readResponseBody(new Response(body), { signal: controller.signal });
  setImmediate(() => controller.abort(reason));

  await assert.rejects(reading, reason);
  assert.equal(canceled, true);
});

test("health probe 不会超过整体 deadline", async () => {
  let probeSignal;
  const startedAt = Date.now();
  const result = await waitForRouterHealth({
    timeoutMs: 40,
    requestTimeoutMs: 500,
    intervalMs: 0,
    fetchImpl: async (_url, { signal }) => {
      probeSignal = signal;
      await new Promise((resolve, reject) => {
        if (signal.aborted) {
          reject(signal.reason);
          return;
        }
        signal.addEventListener("abort", () => reject(signal.reason), { once: true });
      });
    },
  });

  assert.equal(result.ok, false);
  assert.ok(Date.now() - startedAt < 250);
  assert.ok(probeSignal);
  assert.equal(probeSignal.reason?.name, "TimeoutError");
});

test("timeoutMs=0 仍允许一次完整 probe", async () => {
  const result = await waitForRouterHealth({
    timeoutMs: 0,
    requestTimeoutMs: 100,
    fetchImpl: async (_url, { signal }) => {
      await new Promise((resolve, reject) => {
        const timer = setTimeout(resolve, 10);
        signal.addEventListener("abort", () => {
          clearTimeout(timer);
          reject(signal.reason);
        }, { once: true });
      });
      return new Response(JSON.stringify({ service: "codex-router" }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      });
    },
  });
  assert.deepEqual(result, {
    ok: true,
    payload: { service: "codex-router" },
  });
});
