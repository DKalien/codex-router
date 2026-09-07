import assert from "node:assert/strict";
import { Readable } from "node:stream";
import test from "node:test";

import { readRequestBody, readResponseBody, zstdFrameContentSize } from "../src/http-utils.mjs";
import { waitForRouterHealth } from "../src/router-health.mjs";

test("zstd 帧头尺寸覆盖字段宽度、字典和窗口偏移及截断输入", () => {
  for (const single of [false, true]) {
    for (let flag = 0; flag < 4; flag += 1) {
      for (let dictionary = 0; dictionary < 4; dictionary += 1) {
        const width = flag === 0 ? (single ? 1 : 0) : [0, 2, 4, 8][flag];
        const offset = 5 + (single ? 0 : 1) + [0, 1, 2, 4][dictionary];
        const frame = Buffer.alloc(offset + width);
        frame.writeUInt32LE(0xfd2fb528);
        frame[4] = (flag << 6) | (single ? 0x20 : 0) | dictionary;
        if (width === 8) frame.writeBigUInt64LE(42n, offset);
        else if (width) frame.writeUIntLE(42, offset, width);
        assert.equal(zstdFrameContentSize(frame), width ? (width === 2 ? 298 : 42) : undefined);
        for (let length = 0; length < frame.length; length += 1) {
          assert.equal(zstdFrameContentSize(frame.subarray(0, length)), undefined);
        }
      }
    }
  }
  const huge = Buffer.from([0x28, 0xb5, 0x2f, 0xfd, 0xe0, 255, 255, 255, 255, 255, 255, 255, 255]);
  assert.equal(zstdFrameContentSize(huge), Number.MAX_SAFE_INTEGER);
  assert.equal(zstdFrameContentSize(Buffer.from("not a frame")), undefined);
});

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
