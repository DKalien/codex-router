import assert from "node:assert/strict";
import { spawn, spawnSync } from "node:child_process";
import { once } from "node:events";
import { existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import http from "node:http";
import net from "node:net";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";
import { createZstdCompress, gzipSync, zstdCompressSync, zstdDecompressSync } from "node:zlib";

import { ResponseUsageTransform } from "../src/response-usage.mjs";
import { readResponseTextLimited } from "../src/http-utils.mjs";
import { isUsageExhausted, sanitizeUpstreamText } from "../src/error-translation.mjs";
import { statusIsReady } from "../src/status.mjs";

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const callerKey = "test-router-caller-capability-with-sufficient-length";

async function listen(server) {
  server.listen(0, "127.0.0.1");
  await once(server, "listening");
  return server.address().port;
}

async function close(server) {
  if (server.listening) await new Promise((resolve) => server.close(resolve));
}

async function unusedPort() {
  const server = http.createServer();
  const port = await listen(server);
  await close(server);
  return port;
}

function usageEvents(stateDir) {
  const file = path.join(stateDir, "usage-events.jsonl");
  if (!existsSync(file)) return [];
  return readFileSync(file, "utf8")
    .split("\n")
    .filter(Boolean)
    .map((line) => JSON.parse(line));
}

async function waitUntil(predicate, message, timeoutMs = 3_000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    if (await predicate()) return;
    await new Promise((resolve) => setTimeout(resolve, 25));
  }
  throw new Error(message);
}

async function startRouter(overrides = {}) {
  const routerPort = await unusedPort();
  const stateDir = mkdtempSync(path.join(os.tmpdir(), "codex-router-fixes-"));
  const router = spawn(process.execPath, [path.join(root, "src", "router.mjs")], {
    cwd: root,
    env: {
      ...process.env,
      CODEX_HOME: stateDir,
      MODEL_ROUTER_STATE_DIR: stateDir,
      CODEX_ROUTER_CALLER_KEY: callerKey,
      CODEX_ROUTER_PORT: String(routerPort),
      CODEX_ROUTER_QUIET: "1",
      NODE_USE_ENV_PROXY: "0",
      NO_PROXY: "127.0.0.1,localhost",
      MIMO_API_KEY: "test-mimo-key",
      ...overrides,
    },
    stdio: ["ignore", "ignore", "pipe"],
  });
  let stderr = "";
  const startup = new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error("router did not start")), 5_000);
    router.stderr.on("data", (chunk) => {
      stderr += chunk.toString("utf8");
      if (stderr.includes("[codex-router] listening")) {
        clearTimeout(timer);
        resolve();
      }
    });
    router.once("exit", (code) => {
      clearTimeout(timer);
      reject(new Error(`router exited during startup (${code})`));
    });
  });
  await startup;
  return {
    router,
    routerPort,
    stateDir,
    stderr: () => stderr,
    async close() {
      router.kill("SIGTERM");
      if (router.exitCode === null) await once(router, "exit");
      rmSync(stateDir, { recursive: true, force: true });
    },
  };
}

test("原生 GPT 主代理和子代理分别遵循各自供应商路由，旧命名空间和错误边界保持不变", async () => {
  const received = [];
  const receivedUrls = [];
  const catalog = { models: [{ slug: "gpt-new-model", context_window: 123456, future_capability: true }] };
  const upstream = (provider) => http.createServer(async (request, response) => {
    if (request.method === "GET") {
      received.push({ provider, url: request.url, authorization: request.headers.authorization });
      response.writeHead(200, { "Content-Type": "application/json" });
      response.end(JSON.stringify(catalog));
      return;
    }
    const chunks = [];
    for await (const chunk of request) chunks.push(chunk);
    const body = JSON.parse(Buffer.concat(chunks).toString("utf8"));
    receivedUrls.push(request.url);
    received.push({ provider, model: body.model, authorization: request.headers.authorization });
    if (provider === "wlb" && body.model === "gpt-unsupported") {
      response.writeHead(404, { "Content-Type": "application/json" });
      response.end(JSON.stringify({ error: { message: "Model not supported" } }));
      return;
    }
    response.writeHead(200, { "Content-Type": "application/json" });
    response.end(JSON.stringify({
      output: [{ type: "message", role: "assistant", content: [{ type: "output_text", text: "摘要" }] }],
      usage: { input_tokens: 5, output_tokens: 2 },
    }));
  });
  const official = upstream("official");
  const wlb = upstream("wlb");
  const officialPort = await listen(official);
  const wlbPort = await listen(wlb);
  const router = await startRouter({
    CODEX_NATIVE_BASE_URL: `http://127.0.0.1:${officialPort}`,
    WLB_BASE_URL: `http://127.0.0.1:${wlbPort}`,
    WLB_API_KEY: "test-wlb-key",
    CODEX_ROUTER_SESSIONS_PATH: "",
    CODEX_ROUTER_SESSION_INDEX: "",
  });
  const cli = (...args) => {
    const extraEnv = args.at(-1) && typeof args.at(-1) === "object" ? args.pop() : {};
    return spawnSync(process.execPath, [path.join(root, "src/gpt-route.mjs"), ...args], {
      encoding: "utf8",
      env: { ...process.env, CODEX_HOME: router.stateDir, MODEL_ROUTER_STATE_DIR: router.stateDir, WLB_API_KEY: "test-wlb-key", ...extraEnv },
    });
  };
  const request = async (model, endpoint = "responses", headers = {}, input = [{ role: "user", content: "你好" }]) => {
    const response = await fetch(`http://127.0.0.1:${router.routerPort}/_codex-router/${callerKey}/v1/${endpoint}`, {
      method: "POST",
      headers: { "Content-Type": "application/json", Authorization: "Bearer test-official-key", ...headers },
      body: JSON.stringify({ model, input, stream: false }),
    });
    return { status: response.status, body: await response.json() };
  };
  try {
    assert.match(cli("status").stdout, /official/);
    assert.equal((await request("gpt-5.6-sol")).status, 200);
    assert.equal(received.at(-1).provider, "official");
    assert.equal(received.at(-1).authorization, "Bearer test-official-key");
    assert.equal(cli("wlb", { WLB_API_KEY: "" }).status, 1);
    assert.match(cli("status").stdout, /official/);
    const switched = cli("wlb");
    assert.equal(switched.status, 0, switched.stderr);
    const models = await fetch(`http://127.0.0.1:${router.routerPort}/_codex-router/${callerKey}/v1/models?client_version=test`, {
      headers: { Authorization: "Bearer test-official-key" },
    });
    assert.equal(models.status, 200);
    assert.deepEqual(await models.json(), catalog);
    assert.deepEqual(received.at(-1), { provider: "official", url: "/models?client_version=test", authorization: "Bearer test-official-key" });
    for (const model of ["gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna", "gpt-6-astra", "gpt-new-model"]) {
      assert.equal((await request(model)).status, 200);
      assert.deepEqual(received.at(-1), { provider: "wlb", model, authorization: "Bearer test-wlb-key" });
    }
    assert.equal((await request("gpt-new-model", "responses/compact")).status, 200);
    assert.equal(received.at(-1).provider, "wlb");
    assert.equal(received.at(-1).model, "gpt-new-model");
    assert.equal(cli("subagent", "wlb").status, 0);
    assert.equal((await request("gpt-new-model", "responses", { "x-openai-subagent": "true" })).status, 200);
    assert.equal(received.at(-1).provider, "wlb");
    assert.equal(cli("subagent", "official").status, 0);
    const compactV2Input = [{ role: "user", content: "你好" }, { type: "compaction_trigger" }];
    const endpoints = [["responses", undefined], ["responses/compact", undefined], ["responses", compactV2Input]];
    const parentId = "00000000-0000-4000-8000-000000000001";
    const childId = "00000000-0000-4000-8000-000000000002";
    const markers = [
      { "x-openai-subagent": "true" },
      { "x-openai-subagent": "reviewer" },
      { "x-codex-parent-thread-id": parentId },
      { "x-openai-subagent": "false", "x-codex-parent-thread-id": parentId },
    ];
    for (const [endpoint, input] of endpoints) {
      assert.equal((await request("gpt-new-model", endpoint, {}, input)).status, 200);
      assert.deepEqual(received.at(-1), { provider: "wlb", model: "gpt-new-model", authorization: "Bearer test-wlb-key" });
      for (const marker of markers) {
        assert.equal((await request("gpt-new-model", endpoint, marker, input)).status, 200);
        assert.deepEqual(received.at(-1), { provider: "official", model: "gpt-new-model", authorization: "Bearer test-official-key" });
        assert.equal(receivedUrls.at(-1), `/${endpoint}`);
      }
    }
    for (const headers of [
      { "x-openai-subagent": "" },
      { "x-openai-subagent": " false " },
      { "x-openai-subagent": "0" },
      { "x-openai-subagent": "FALSE", "x-codex-parent-thread-id": "invalid" },
    ]) {
      assert.equal((await request("gpt-new-model", "responses", headers, [{ role: "user", content: "subagent thread_spawn" }])).status, 200);
      assert.equal(received.at(-1).provider, "wlb");
    }
    const beforeUnknownEndpoint = received.length;
    assert.equal((await request("gpt-new-model", "responses/compact/v2", markers[0])).status, 404);
    assert.equal(received.length, beforeUnknownEndpoint);
    const rolloutHeaders = { "thread-id": childId };
    // 同一线程先缺文件，再出现空/未写完首行，最后才写入可识别的子代理元数据。
    assert.equal((await request("gpt-new-model", "responses", rolloutHeaders)).status, 200);
    assert.equal(received.at(-1).provider, "wlb");
    const sessionsPath = path.join(router.stateDir, "sessions");
    mkdirSync(sessionsPath, { recursive: true });
    const rolloutPath = path.join(sessionsPath, `rollout-test-${childId}.jsonl`);
    for (const partial of ["", '{"payload":']) {
      writeFileSync(rolloutPath, partial);
      assert.equal((await request("gpt-new-model", "responses", rolloutHeaders)).status, 200);
      assert.equal(received.at(-1).provider, "wlb");
    }
    writeFileSync(rolloutPath, `${JSON.stringify({ type: "session_meta", payload: { id: childId, source: { subagent: { thread_spawn: { parent_thread_id: parentId } } } } })}\n`);
    for (const [endpoint, input] of endpoints) {
      assert.equal((await request("gpt-new-model", endpoint, rolloutHeaders, input)).status, 200);
      assert.deepEqual(received.at(-1), { provider: "official", model: "gpt-new-model", authorization: "Bearer test-official-key" });
    }
    assert.equal((await request("gpt-new-model", "responses", { ...rolloutHeaders, "x-openai-subagent": "0" })).status, 200);
    assert.equal(received.at(-1).provider, "official");
    const beforeUnsupported = received.length;
    assert.equal((await request("gpt-unsupported")).status, 404);
    assert.equal(received.length, beforeUnsupported + 1);
    assert.equal(received.at(-1).provider, "wlb");
    const beforeFailure = received.length;
    writeFileSync(path.join(router.stateDir, "gpt-route.json"), "broken");
    assert.equal((await request("gpt-5.6-sol")).status, 503);
    assert.equal(received.length, beforeFailure);
    for (const marker of [...markers, rolloutHeaders]) {
      assert.equal((await request("gpt-5.6-sol", "responses", marker)).status, 200);
      assert.deepEqual(received.at(-1), { provider: "official", model: "gpt-5.6-sol", authorization: "Bearer test-official-key" });
    }
    assert.equal(cli("official").status, 0);
    assert.equal((await request("gpt-unregistered")).status, 200);
    assert.equal(received.at(-1).provider, "official");
    assert.equal((await request("wlb-relay/gpt-5.6-sol", "responses", markers[0])).status, 200);
    assert.equal(received.at(-1).provider, "wlb");
    assert.equal(cli("invalid").status, 2);
    assert.equal(cli("subagent", "invalid").status, 2);
    assert.match(cli("status").stdout, /official/);
  } finally {
    await router.close();
    await close(official);
    await close(wlb);
  }
});

test("zstd 请求解压覆盖官方及第三方入口，并提前拒绝超大帧", async () => {
  const received = [];
  const upstream = http.createServer(async (request, response) => {
    const chunks = [];
    for await (const chunk of request) chunks.push(chunk);
    received.push(JSON.parse(Buffer.concat(chunks).toString("utf8")));
    response.writeHead(200, { "Content-Type": "application/json" });
    response.end(JSON.stringify({ output: [], usage: { input_tokens: 1, output_tokens: 0 } }));
  });
  const port = await listen(upstream);
  const router = await startRouter({
    CODEX_NATIVE_BASE_URL: `http://127.0.0.1:${port}`,
    WLB_BASE_URL: `http://127.0.0.1:${port}`,
    WLB_API_KEY: "test-wlb-key",
    MODEL_ROUTER_MAX_DECODED_BODY_BYTES: "1024",
  });
  const send = async (endpoint, body, encoding = "zstd") => {
    const response = await fetch(`http://127.0.0.1:${router.routerPort}/_codex-router/${callerKey}/v1/${endpoint}`, {
      method: "POST",
      headers: { "Content-Type": "application/json", "Content-Encoding": encoding, Authorization: "Bearer test-official" },
      body,
    });
    await response.arrayBuffer();
    return response.status;
  };
  try {
    for (const [endpoint, model] of [["responses", "gpt-5.6-sol"], ["responses", "wlb-relay/gpt-5.6-sol"], ["alpha/search", "gpt-5.6-sol"]]) {
      const payload = Buffer.from(JSON.stringify({ model, input: "你好", stream: false }));
      assert.equal(await send(endpoint, zstdCompressSync(payload)), 200);
      assert.equal(received.at(-1).input, "你好");
      assert.equal(await send(endpoint, gzipSync(zstdCompressSync(payload)), "zstd, gzip"), 200);
      // 只有帧头，没有可解码数据：413 证明在原生解码器报 400 前检查了尺寸。
      const oversizedHeader = Buffer.from([0x28, 0xb5, 0x2f, 0xfd, 0xa0, 0, 8, 0, 0]);
      const before = received.length;
      assert.equal(await send(endpoint, oversizedHeader), 413);
      assert.equal(await send(endpoint, Buffer.from("bad zstd")), 400);
      assert.equal(received.length, before);
    }
    assert.equal(await send("responses", zstdCompressSync(Buffer.alloc(2048, 65))), 413);
    // 流式编码不声明输出尺寸，仍必须由解码器的 maxOutputLength 拒绝。
    const encoder = createZstdCompress();
    encoder.end(Buffer.alloc(2048, 65));
    const chunks = [];
    for await (const chunk of encoder) chunks.push(chunk);
    const unknownSize = Buffer.concat(chunks);
    assert.equal(unknownSize[4] & 0xe0, 0);
    assert.equal(await send("responses", unknownSize), 413);
  } finally {
    await router.close();
    await close(upstream);
  }
});

test("孤立 app 工具结果在第三方普通及压缩请求保留，官方和其他工具保持原样", async () => {
  const received = [];
  const upstream = http.createServer(async (request, response) => {
    const chunks = [];
    for await (const chunk of request) chunks.push(chunk);
    received.push(JSON.parse(Buffer.concat(chunks).toString("utf8")));
    response.writeHead(200, { "Content-Type": "application/json" });
    response.end(JSON.stringify({ output: [{ type: "message", role: "assistant", content: [{ type: "output_text", text: "摘要" }] }] }));
  });
  const port = await listen(upstream);
  const router = await startRouter({ CODEX_NATIVE_BASE_URL: `http://127.0.0.1:${port}`, MIMO_BASE_URL: `http://127.0.0.1:${port}`, WLB_BASE_URL: `http://127.0.0.1:${port}`, WLB_API_KEY: "test-wlb-key" });
  const orphan = { type: "function_call_output", namespace: "codex_app", name: "read_thread", output: { text: "保留结果" } };
  const untouched = [
    { ...orphan, call_id: "existing-call" },
    { ...orphan, namespace: "other" },
    { type: "function_call_output", output: "无名称" },
    { type: "function_call_output", namespace: "codex_app", name: "read_thread" },
  ];
  const input = [orphan, { ...orphan, call_id: "", output: "字符串结果" }, ...untouched];
  try {
    for (const model of ["gpt-5.6-sol", "wlb-relay/gpt-5.6-sol", "mimo-token-plan/mimo-v2.5-pro"]) {
      for (const endpoint of ["responses", "responses/compact"]) {
        const response = await fetch(`http://127.0.0.1:${router.routerPort}/_codex-router/${callerKey}/v1/${endpoint}`, {
          method: "POST", headers: { "Content-Type": "application/json", Authorization: "Bearer test-official" },
          body: JSON.stringify({ model, input, stream: false }),
        });
        assert.equal(response.status, 200, await response.text());
        const actual = received.at(-1).input;
        if (model.startsWith("gpt-")) assert.deepEqual(actual, input);
        else {
          assert.equal(actual[0].type, "message");
          assert.equal(actual[0].role, "user");
          assert.match(actual[0].content[0].text, /codex_app\.read_thread/);
          assert.ok(actual[0].content[0].text.endsWith(JSON.stringify(orphan.output)));
          assert.ok(actual[1].content[0].text.endsWith("字符串结果"));
          assert.deepEqual(actual.slice(2, 6), untouched);
        }
      }
    }
  } finally {
    await router.close();
    await close(upstream);
  }
});

test("无 content-type 的 SSE 仍能统计 Token 并原样透传", async () => {
  const body =
    'event: response.completed\ndata: {"type":"response.completed","response":{"usage":{"input_tokens":7,"output_tokens":3}}}\n\n';
  const transform = new ResponseUsageTransform("");
  const output = [];
  transform.on("data", (chunk) => output.push(chunk));
  transform.end(Buffer.from(body));
  await once(transform, "end");

  assert.equal(Buffer.concat(output).toString("utf8"), body);
  assert.deepEqual(transform.tokenUsage(), {
    inputTokens: 7,
    outputTokens: 3,
    totalTokens: 10,
  });
  assert.equal(transform.completedResponseObserved(), true);
});

test("原生 GPT-5.6 删除旧 prompt_cache_retention 并保留 prompt_cache_options", async () => {
  let upstreamBody;
  const native = http.createServer(async (request, response) => {
    const chunks = [];
    for await (const chunk of request) chunks.push(chunk);
    upstreamBody = JSON.parse(Buffer.concat(chunks).toString("utf8"));
    response.writeHead(200, { "Content-Type": "application/json" });
    response.end('{"output":[]}');
  });
  const nativePort = await listen(native);
  const router = await startRouter({
    CODEX_NATIVE_BASE_URL: `http://127.0.0.1:${nativePort}/backend-api/codex`,
  });

  try {
    const response = await fetch(
      `http://127.0.0.1:${router.routerPort}/_codex-router/${callerKey}/v1/responses`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          model: "gpt-5.6-sol",
          input: "cache compatibility",
          prompt_cache_retention: "24h",
          prompt_cache_options: { retention: "24h" },
          stream: false,
        }),
      },
    );
    assert.equal(response.status, 200, await response.text());
    assert.equal(upstreamBody.prompt_cache_retention, undefined);
    assert.deepEqual(upstreamBody.prompt_cache_options, { retention: "24h" });
  } finally {
    await router.close();
    await close(native);
  }
});

test("native 丢弃不可回放 reasoning 并保留合法 reasoning 和普通消息", async () => {
  let upstreamBody;
  const native = http.createServer(async (request, response) => {
    const chunks = [];
    for await (const chunk of request) chunks.push(chunk);
    upstreamBody = JSON.parse(Buffer.concat(chunks).toString("utf8"));
    response.writeHead(200, { "Content-Type": "application/json" });
    response.end('{"output":[]}');
  });
  const nativePort = await listen(native);
  const router = await startRouter({
    CODEX_NATIVE_BASE_URL: `http://127.0.0.1:${nativePort}/backend-api/codex`,
  });
  const opaqueEncryptedContent = "gAAAAABnative-opaque";

  try {
    const response = await fetch(
      `http://127.0.0.1:${router.routerPort}/_codex-router/${callerKey}/v1/responses`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          model: "gpt-5.6-sol",
          input: [
            {
              type: "reasoning",
              id: "rs_mimo",
              summary: [],
              content: [{ type: "reasoning_text", text: "MiMo reasoning" }],
              encrypted_content: null,
            },
            { type: "reasoning", id: "rs_missing", summary: [] },
            {
              type: "reasoning",
              id: "rs_plain",
              summary: [],
              encrypted_content: "plaintext-token",
            },
            {
              type: "reasoning",
              id: "rs_native",
              summary: [{ type: "summary_text", text: "Native summary" }],
              content: [{ type: "reasoning_text", text: "output-only content" }],
              encrypted_content: opaqueEncryptedContent,
            },
            {
              type: "message",
              role: "user",
              content: [{ type: "input_text", text: "ordinary user message" }],
            },
          ],
          stream: false,
        }),
      },
    );
    assert.equal(response.status, 200, await response.text());
    assert.deepEqual(upstreamBody.input, [
      {
        type: "reasoning",
        id: "rs_native",
        summary: [{ type: "summary_text", text: "Native summary" }],
        encrypted_content: opaqueEncryptedContent,
      },
      {
        type: "message",
        role: "user",
        content: [{ type: "input_text", text: "ordinary user message" }],
      },
    ]);
  } finally {
    await router.close();
    await close(native);
  }
});

function cancelNativeTurnAfterMarker(port, body, marker) {
  return new Promise((resolve) => {
    const request = http.request(
      {
        host: "127.0.0.1",
        port,
        path: `/_codex-router/${callerKey}/v1/responses`,
        method: "POST",
        headers: { "Content-Type": "application/json" },
      },
      (response) => {
        let received = "";
        response.setEncoding("utf8");
        response.on("data", (chunk) => {
          received += chunk;
          if (received.includes(marker)) {
            request.destroy();
            resolve(received);
          }
        });
      },
    );
    request.once("error", () => resolve(""));
    request.end(JSON.stringify(body));
  });
}

test("native 在 response.completed 后断开仍按上游状态和用量成功计量", async () => {
  const native = http.createServer((_request, response) => {
    response.writeHead(200);
    response.write(
      [
        "event: response.completed",
        `data: ${JSON.stringify({
          type: "response.completed",
          response: { usage: { input_tokens: 40, output_tokens: 4, total_tokens: 44 } },
        })}`,
        "",
        "",
      ].join("\n"),
    );
  });
  const nativePort = await listen(native);
  const router = await startRouter({
    CODEX_NATIVE_BASE_URL: `http://127.0.0.1:${nativePort}/backend-api/codex`,
    CODEX_ROUTER_NATIVE_RETRIES: "0",
  });

  try {
    await cancelNativeTurnAfterMarker(
      router.routerPort,
      { model: "gpt-5.6-sol", input: "complete", stream: true },
      '"type":"response.completed"',
    );
    await waitUntil(() => usageEvents(router.stateDir).length > 0, "未记录 native 用量");
    const event = usageEvents(router.stateDir).at(-1);
    assert.equal(event.provider, "openai");
    assert.equal(event.status, 200);
    assert.equal(event.inputTokens, 40);
    assert.equal(event.outputTokens, 4);
  } finally {
    await router.close();
    await close(native);
  }
});

test("native 在 response.completed 前断开仍计量为 0", async () => {
  const native = http.createServer((_request, response) => {
    response.writeHead(200, { "Content-Type": "text/event-stream" });
    response.write(
      'event: response.output_text.delta\ndata: {"type":"response.output_text.delta","delta":"partial"}\n\n',
    );
  });
  const nativePort = await listen(native);
  const router = await startRouter({
    CODEX_NATIVE_BASE_URL: `http://127.0.0.1:${nativePort}/backend-api/codex`,
    CODEX_ROUTER_NATIVE_RETRIES: "0",
  });

  try {
    await cancelNativeTurnAfterMarker(
      router.routerPort,
      { model: "gpt-5.6-sol", input: "cancel", stream: true },
      "partial",
    );
    await waitUntil(() => usageEvents(router.stateDir).length > 0, "未记录 native 用量");
    assert.equal(usageEvents(router.stateDir).at(-1).status, 0);
  } finally {
    await router.close();
    await close(native);
  }
});

test("独立 Web Search 请求只转发给原生后端", async () => {
  const requests = [];
  const native = http.createServer(async (request, response) => {
    const chunks = [];
    for await (const chunk of request) chunks.push(chunk);
    requests.push({
      url: request.url,
      authorization: request.headers.authorization,
      body: JSON.parse(Buffer.concat(chunks).toString("utf8")),
    });
    response.writeHead(200, { "Content-Type": "application/json" });
    response.end('{"output":"search result"}');
  });
  const nativePort = await listen(native);
  const routerPort = await unusedPort();
  const stateDir = mkdtempSync(path.join(os.tmpdir(), "codex-router-fixes-"));
  const router = spawn(process.execPath, [path.join(root, "src", "router.mjs")], {
    cwd: root,
    env: {
      ...process.env,
      CODEX_HOME: stateDir,
      MODEL_ROUTER_STATE_DIR: stateDir,
      CODEX_ROUTER_CALLER_KEY: callerKey,
      CODEX_ROUTER_PORT: String(routerPort),
      CODEX_NATIVE_BASE_URL: `http://127.0.0.1:${nativePort}/backend-api/codex`,
      CODEX_ROUTER_QUIET: "1",
      NODE_USE_ENV_PROXY: "0",
      NO_PROXY: "127.0.0.1,localhost",
    },
    stdio: ["ignore", "ignore", "pipe"],
  });

  try {
    const [startup] = await once(router.stderr, "data", {
      signal: AbortSignal.timeout(5_000),
    });
    assert.match(startup.toString("utf8"), /\[codex-router\] listening/);

    const unauthenticated = await fetch(
      `http://127.0.0.1:${routerPort}/_codex-router/${callerKey}/v1/alpha/search?source=test`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          model: "gpt-5.6-sol",
          commands: { search_query: [{ q: "should not reach upstream" }] },
        }),
      },
    );
    assert.equal(unauthenticated.status, 401);
    assert.deepEqual(await unauthenticated.json(), {
      error: {
        type: "native_session_required",
        message: "This native OpenAI route requires an active ChatGPT/Codex session.",
      },
    });
    assert.deepEqual(requests, []);

    const unauthenticatedImage = await fetch(
      `http://127.0.0.1:${routerPort}/_codex-router/${callerKey}/v1/images/generations`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ prompt: "should not reach upstream" }),
      },
    );
    assert.equal(unauthenticatedImage.status, 401);
    assert.deepEqual(await unauthenticatedImage.json(), {
      error: {
        type: "native_session_required",
        message: "This native OpenAI route requires an active ChatGPT/Codex session.",
      },
    });
    assert.deepEqual(requests, []);

    const response = await fetch(
      `http://127.0.0.1:${routerPort}/_codex-router/${callerKey}/v1/alpha/search?source=test`,
      {
        method: "POST",
        headers: {
          Authorization: "Bearer native-test-token",
          "Content-Type": "application/json",
        },
        body: JSON.stringify({
          model: "gpt-5.6-sol",
          commands: { search_query: [{ q: "OpenAI news" }] },
        }),
      },
    );

    assert.equal(response.status, 200);
    assert.deepEqual(await response.json(), { output: "search result" });
    assert.deepEqual(requests, [
      {
        url: "/backend-api/codex/alpha/search?source=test",
        authorization: "Bearer native-test-token",
        body: {
          model: "gpt-5.6-sol",
          commands: { search_query: [{ q: "OpenAI news" }] },
        },
      },
    ]);
  } finally {
    router.kill("SIGTERM");
    if (router.exitCode === null) await once(router, "exit");
    await close(native);
    rmSync(stateDir, { recursive: true, force: true });
  }
});

test("MiMo 请求不会透传不支持的托管 Web Search 工具", async () => {
  let upstreamBody;
  const mimo = http.createServer(async (request, response) => {
    const chunks = [];
    for await (const chunk of request) chunks.push(chunk);
    upstreamBody = JSON.parse(Buffer.concat(chunks).toString("utf8"));
    response.writeHead(200, { "Content-Type": "application/json" });
    response.end('{"status":"completed","output":[]}');
  });
  const mimoPort = await listen(mimo);
  const routerPort = await unusedPort();
  const stateDir = mkdtempSync(path.join(os.tmpdir(), "codex-router-fixes-"));
  const router = spawn(process.execPath, [path.join(root, "src", "router.mjs")], {
    cwd: root,
    env: {
      ...process.env,
      CODEX_HOME: stateDir,
      MODEL_ROUTER_STATE_DIR: stateDir,
      CODEX_ROUTER_CALLER_KEY: callerKey,
      CODEX_ROUTER_PORT: String(routerPort),
      CODEX_ROUTER_QUIET: "1",
      MIMO_API_KEY: "test-mimo-key",
      MIMO_BASE_URL: `http://127.0.0.1:${mimoPort}`,
      NODE_USE_ENV_PROXY: "0",
      NO_PROXY: "127.0.0.1,localhost",
    },
    stdio: ["ignore", "ignore", "pipe"],
  });

  try {
    const [startup] = await once(router.stderr, "data", {
      signal: AbortSignal.timeout(5_000),
    });
    assert.match(startup.toString("utf8"), /\[codex-router\] listening/);

    const response = await fetch(
      `http://127.0.0.1:${routerPort}/_codex-router/${callerKey}/v1/responses`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          model: "mimo-token-plan/mimo-v2.5-pro",
          input: "check upstream updates",
          tools: [
            { type: "web_search", external_web_access: false },
            {
              type: "function",
              name: "exec_command",
              description: "Run a command",
              parameters: { type: "object", properties: {} },
            },
          ],
          tool_choice: "auto",
          stream: false,
        }),
      },
    );
    const responseBody = await response.text();

    assert.equal(response.status, 200, responseBody);
    assert.equal(JSON.parse(responseBody).status, "completed");
    assert.equal(upstreamBody.model, "mimo-v2.5-pro");
    assert.deepEqual(upstreamBody.tools.map((tool) => tool.type), ["function"]);
  } finally {
    router.kill("SIGTERM");
    if (router.exitCode === null) await once(router, "exit");
    await close(mimo);
    rmSync(stateDir, { recursive: true, force: true });
  }
});

test("MiMo 将 custom tool 历史桥接为 function 并还原流式调用", async () => {
  let upstreamBody;
  const mimo = http.createServer(async (request, response) => {
    const chunks = [];
    for await (const chunk of request) chunks.push(chunk);
    upstreamBody = JSON.parse(Buffer.concat(chunks).toString("utf8"));
    const events = [
      {
        type: "response.output_item.added",
        output_index: 0,
        item: {
          id: "fc_exec",
          type: "function_call",
          status: "in_progress",
          name: "exec",
          call_id: "call_exec",
          arguments: "",
        },
      },
      {
        type: "response.function_call_arguments.delta",
        item_id: "fc_exec",
        output_index: 0,
        delta: '{"input":"echo ',
      },
      {
        type: "response.function_call_arguments.delta",
        item_id: "fc_exec",
        output_index: 0,
        delta: 'hello"}',
      },
      {
        type: "response.function_call_arguments.done",
        item_id: "fc_exec",
        output_index: 0,
        arguments: '{"input":"echo hello"}',
      },
      {
        type: "response.output_item.done",
        output_index: 0,
        item: {
          id: "fc_exec",
          type: "function_call",
          status: "completed",
          name: "exec",
          call_id: "call_exec",
          arguments: '{"input":"echo hello"}',
        },
      },
      {
        type: "response.output_item.added",
        output_index: 1,
        item: {
          id: "fc_plain",
          type: "function_call",
          status: "in_progress",
          name: "ordinary",
          call_id: "call_plain",
          arguments: "",
        },
      },
      {
        type: "response.function_call_arguments.delta",
        item_id: "fc_plain",
        output_index: 1,
        delta: '{"value":"ok"}',
      },
      {
        type: "response.function_call_arguments.done",
        item_id: "fc_plain",
        output_index: 1,
        arguments: '{"value":"ok"}',
      },
      {
        type: "response.output_item.done",
        output_index: 1,
        item: {
          id: "fc_plain",
          type: "function_call",
          status: "completed",
          name: "ordinary",
          call_id: "call_plain",
          arguments: '{"value":"ok"}',
        },
      },
      {
        type: "response.completed",
        response: { status: "completed", output: [] },
      },
    ];
    response.writeHead(200, { "Content-Type": "text/event-stream" });
    events.forEach((event, sequence_number) => {
      response.write(`event: ${event.type}\ndata: ${JSON.stringify({ ...event, sequence_number })}\n\n`);
    });
    response.end("data: [DONE]\n\n");
  });
  const mimoPort = await listen(mimo);
  const routerPort = await unusedPort();
  const stateDir = mkdtempSync(path.join(os.tmpdir(), "codex-router-fixes-"));
  const router = spawn(process.execPath, [path.join(root, "src", "router.mjs")], {
    cwd: root,
    env: {
      ...process.env,
      CODEX_HOME: stateDir,
      MODEL_ROUTER_STATE_DIR: stateDir,
      CODEX_ROUTER_CALLER_KEY: callerKey,
      CODEX_ROUTER_PORT: String(routerPort),
      CODEX_ROUTER_QUIET: "1",
      MIMO_API_KEY: "test-mimo-key",
      MIMO_BASE_URL: `http://127.0.0.1:${mimoPort}`,
      NODE_USE_ENV_PROXY: "0",
      NO_PROXY: "127.0.0.1,localhost",
    },
    stdio: ["ignore", "ignore", "pipe"],
  });

  try {
    const [startup] = await once(router.stderr, "data", {
      signal: AbortSignal.timeout(5_000),
    });
    assert.match(startup.toString("utf8"), /\[codex-router\] listening/);

    const response = await fetch(
      `http://127.0.0.1:${routerPort}/_codex-router/${callerKey}/v1/responses`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          model: "mimo-token-plan/mimo-v2.5-pro",
          input: [
            {
              type: "custom_tool_call",
              id: "hist_exec",
              status: "completed",
              call_id: "hist_call_exec",
              name: "exec",
              input: "pwd",
            },
            {
              type: "custom_tool_call_output",
              id: "hist_exec_output",
              status: "completed",
              call_id: "hist_call_exec",
              output: "C:/work",
            },
            {
              type: "agent_message",
              id: "hist_agent",
              status: "completed",
              author: "agent-a",
              recipient: "agent-b",
              internal_chat_message_metadata_passthrough: { trace: "drop-me" },
              content: [
                {
                  type: "input_text",
                  text: "Message Type: FINAL_ANSWER\nPayload:",
                },
                { type: "encrypted_content", encrypted_content: "visible handoff" },
                { type: "output_text", text: "drop this extension content" },
              ],
            },
            {
              type: "message",
              role: "user",
              content: [{ type: "input_text", text: "ordinary message" }],
            },
            {
              type: "function_call",
              id: "hist_plain",
              status: "completed",
              call_id: "hist_call_plain",
              name: "ordinary",
              arguments: '{"value":"old"}',
            },
          ],
          tools: [
            {
              type: "custom",
              name: "exec",
              description: "Run a command",
              format: { type: "grammar", syntax: "lark", definition: "start: /.+/" },
            },
            {
              type: "function",
              name: "ordinary",
              description: "An ordinary function",
              parameters: {
                type: "object",
                properties: { value: { type: "string" } },
                required: ["value"],
              },
            },
          ],
          stream: true,
        }),
      },
    );
    const responseBody = await response.text();
    assert.equal(response.status, 200, responseBody);

    const outputEvents = responseBody
      .split(/\r?\n\r?\n/)
      .map((block) => block
        .split(/\r?\n/)
        .find((line) => line.startsWith("data:")))
      .filter((line) => line && line.slice(5).trim() !== "[DONE]")
      .map((line) => JSON.parse(line.slice(5)));
    const execAdded = outputEvents.find(
      (event) => event.type === "response.output_item.added" && event.item?.id === "fc_exec",
    );
    assert.equal(execAdded.item.type, "custom_tool_call");
    assert.equal(execAdded.item.input, "");
    const execDelta = outputEvents.find(
      (event) => event.type === "response.custom_tool_call_input.delta",
    );
    assert.equal(execDelta.delta, "echo hello");
    const execDone = outputEvents.find(
      (event) => event.type === "response.custom_tool_call_input.done",
    );
    assert.equal(execDone.input, "echo hello");
    const execItemDone = outputEvents.find(
      (event) => event.type === "response.output_item.done" && event.item?.id === "fc_exec",
    );
    assert.equal(execItemDone.item.type, "custom_tool_call");
    assert.equal(execItemDone.item.input, "echo hello");
    assert.equal(
      outputEvents.some(
        (event) => event.type === "response.function_call_arguments.done" && event.item_id === "fc_exec",
      ),
      false,
    );
    const ordinaryDone = outputEvents.find(
      (event) => event.type === "response.output_item.done" && event.item?.id === "fc_plain",
    );
    assert.equal(ordinaryDone.item.type, "function_call");
    assert.equal(ordinaryDone.item.name, "ordinary");
    assert.equal(ordinaryDone.item.arguments, '{"value":"ok"}');

    const execTool = upstreamBody.tools.find((tool) => tool.name === "exec");
    assert.deepEqual(execTool, {
      type: "function",
      name: "exec",
      description: "Run a command",
      parameters: {
        type: "object",
        properties: { input: { type: "string" } },
        required: ["input"],
        additionalProperties: false,
      },
    });
    assert.deepEqual(
      upstreamBody.tools.find((tool) => tool.name === "ordinary"),
      {
        type: "function",
        name: "ordinary",
        description: "An ordinary function",
        parameters: {
          type: "object",
          properties: { value: { type: "string" } },
          required: ["value"],
        },
      },
    );
    const historyExec = upstreamBody.input.find((item) => item.id === "hist_exec");
    assert.equal(historyExec.type, "function_call");
    assert.equal(historyExec.call_id, "hist_call_exec");
    assert.equal(historyExec.name, "exec");
    assert.deepEqual(JSON.parse(historyExec.arguments), { input: "pwd" });
    const historyOutput = upstreamBody.input.find((item) => item.id === "hist_exec_output");
    assert.equal(historyOutput.type, "function_call_output");
    assert.equal(historyOutput.call_id, "hist_call_exec");
    assert.equal(historyOutput.output, "C:/work");
    assert.deepEqual(upstreamBody.input.find((item) => item.id === "hist_plain"), {
      type: "function_call",
      id: "hist_plain",
      status: "completed",
      call_id: "hist_call_plain",
      name: "ordinary",
      arguments: '{"value":"old"}',
    });
    assert.equal(upstreamBody.input.some((item) => item.type === "agent_message"), false);
    assert.deepEqual(
      upstreamBody.input.find((item) =>
        item.type === "message" &&
        item.content?.some((part) => part.text === "visible handoff"),
      ),
      {
        type: "message",
        role: "user",
        content: [
          { type: "input_text", text: "Message Type: FINAL_ANSWER\nPayload:" },
          { type: "input_text", text: "visible handoff" },
        ],
      },
    );
    assert.deepEqual(
      upstreamBody.input.find((item) =>
        item.type === "message" &&
        item.content?.some((part) => part.text === "ordinary message"),
      ),
      {
        type: "message",
        role: "user",
        content: [{ type: "input_text", text: "ordinary message" }],
      },
    );
    assert.equal(
      upstreamBody.input.some((item) =>
        item.type === "message" &&
        item.content?.some((part) => part.type === "encrypted_content"),
      ),
      false,
    );
  } finally {
    router.kill("SIGTERM");
    if (router.exitCode === null) await once(router, "exit");
    await close(mimo);
    rmSync(stateDir, { recursive: true, force: true });
  }
});

test("agent_message relay 有界并发、按序保留并命中缓存", async () => {
  let activeRelays = 0;
  let maxInFlight = 0;
  let relayCount = 0;
  const native = http.createServer(async (request, response) => {
    const chunks = [];
    for await (const chunk of request) chunks.push(chunk);
    const body = JSON.parse(Buffer.concat(chunks).toString("utf8"));
    activeRelays += 1;
    maxInFlight = Math.max(maxInFlight, activeRelays);
    relayCount += 1;
    await new Promise((resolve) => setTimeout(resolve, 10));
    activeRelays -= 1;
    const itemId = body.input?.[0]?.id;
    response.writeHead(200, { "Content-Type": "application/json" });
    response.end(JSON.stringify({
      output: [{
        type: "function_call",
        name: "relay_external_agent_payload",
        arguments: JSON.stringify({ payload: `plain-${itemId}` }),
      }],
    }));
  });
  const nativePort = await listen(native);
  const mimoBodies = [];
  const mimo = http.createServer(async (request, response) => {
    const chunks = [];
    for await (const chunk of request) chunks.push(chunk);
    mimoBodies.push(JSON.parse(Buffer.concat(chunks).toString("utf8")));
    response.writeHead(200, { "Content-Type": "application/json" });
    response.end('{"status":"completed","output":[]}');
  });
  const mimoPort = await listen(mimo);
  const router = await startRouter({
    CODEX_NATIVE_BASE_URL: `http://127.0.0.1:${nativePort}/backend-api/codex`,
    MIMO_BASE_URL: `http://127.0.0.1:${mimoPort}`,
  });
  const payloadCount = 263;
  const input = Array.from({ length: payloadCount }, (_, index) => ({
    type: "agent_message",
    id: `agent-${index}`,
    content: [
      { type: "input_text", text: "Message Type: NEW_TASK\nPayload:" },
      { type: "encrypted_content", encrypted_content: `gAAAAA${index}` },
    ],
  }));
  const expected = input.map((item) => `plain-${item.id}`);
  async function send(items) {
    const response = await fetch(
      `http://127.0.0.1:${router.routerPort}/_codex-router/${callerKey}/v1/responses`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          model: "mimo-token-plan/mimo-v2.5-pro",
          input: items,
          stream: false,
        }),
      },
    );
    const body = await response.text();
    assert.equal(response.status, 200, body);
  }

  try {
    await send(input);
    assert.ok(maxInFlight > 1, `expected concurrent relays, got ${maxInFlight}`);
    assert.ok(maxInFlight <= 4, `relay concurrency exceeded limit: ${maxInFlight}`);
    assert.equal(relayCount, payloadCount);
    assert.deepEqual(
      mimoBodies[0].input.map((item) => item.content?.at(-1)?.text),
      expected,
    );

    await send(input);
    assert.equal(relayCount, payloadCount);
    assert.deepEqual(
      mimoBodies[1].input.map((item) => item.content?.at(-1)?.text),
      expected,
    );

    const completed = [0, 1].map((index) => ({
      type: "agent_message",
      id: `completed-${index}`,
      author: "agent-completed",
      content: [
        { type: "input_text", text: "Message Type: MESSAGE\nPayload:" },
        { type: "encrypted_content", encrypted_content: `gAAAAAcompleted${index}` },
      ],
    }));
    completed.push({
      type: "agent_message",
      id: "completed-final",
      author: "agent-completed",
      content: [
        { type: "input_text", text: "Message Type: FINAL_ANSWER\nPayload:" },
        { type: "encrypted_content", encrypted_content: "gAAAAAcompleted-final" },
      ],
    });
    await send(completed);
    assert.equal(relayCount, payloadCount + 1);
    assert.deepEqual(
      mimoBodies[2].input.map((item) => item.content?.at(-1)?.text),
      ["plain-completed-final"],
    );

    const latest = [completed[2], {
      type: "agent_message",
      id: "completed-latest",
      author: "agent-completed",
      content: [
        { type: "input_text", text: "Message Type: MESSAGE\nPayload:" },
        { type: "encrypted_content", encrypted_content: "gAAAAAcompleted-latest" },
      ],
    }];
    await send(latest);
    assert.equal(relayCount, payloadCount + 1);
    assert.deepEqual(mimoBodies[3].input.map((item) => item.content?.at(-1)?.text), [
      "plain-completed-final",
    ]);

    const retained = [
      {
        type: "agent_message",
        id: "retained-new-task",
        content: [{ type: "input_text", text: "Message Type: NEW_TASK\nPayload:" }, { type: "encrypted_content", encrypted_content: "gAAAAAretained-new-task" }],
      },
      {
        type: "agent_message",
        id: "retained-followup",
        content: [{ type: "input_text", text: "Message Type: FOLLOWUP_TASK\nPayload:" }, { type: "encrypted_content", encrypted_content: "gAAAAAretained-followup" }],
      },
      {
        type: "agent_message",
        id: "retained-unknown",
        content: [{ type: "input_text", text: "handoff without recognized type" }, { type: "encrypted_content", encrypted_content: "gAAAAAretained-unknown" }],
      },
    ];
    await send(retained);
    assert.equal(relayCount, payloadCount + 3);
    assert.equal(mimoBodies[4].input.length, retained.length);
    assert.deepEqual(mimoBodies[4].input.slice(0, 2).map((item) => item.content?.at(-1)?.text), [
      "plain-retained-new-task",
      "plain-retained-followup",
    ]);
    assert.match(mimoBodies[4].input[2].content?.[0]?.text || "", /handoff without recognized type/);
  } finally {
    await router.close();
    await close(native);
    await close(mimo);
  }
});

test("上游错误读取达到上限后会取消余流", async () => {
  let cancelReason;
  const response = new Response(
    new ReadableStream({
      start(controller) {
        controller.enqueue(new TextEncoder().encode("x".repeat(128)));
      },
      cancel(reason) {
        cancelReason = reason;
      },
    }),
  );

  const output = await readResponseTextLimited(response, 32);
  assert.equal(Buffer.byteLength(output), 32);
  assert.match(cancelReason, /limit reached/);
});

test("带空格的 quoted secret 脱敏后仍是合法 JSON", () => {
  const sanitized = sanitizeUpstreamText(
    JSON.stringify({ password: "abc 123", token: "xyz 789", error: "sk-abc123" }),
  );
  assert.deepEqual(JSON.parse(sanitized), {
    password: "[REDACTED]",
    token: "[REDACTED]",
    error: "[REDACTED]",
  });
});

test("status 只接受精确目录、配置和 provider 状态", () => {
  const status = {
    health: { ok: true },
    config: { managed: true, catalogConfigured: true },
    catalog: { readable: true, exact: true },
    providers: { selection: {} },
  };
  assert.equal(statusIsReady(status, true), true);
  assert.equal(statusIsReady({ ...status, catalog: { readable: true, exact: false } }, true), false);
  assert.equal(
    statusIsReady({ ...status, providers: { selection: { degraded: "invalid state" } } }, true),
    false,
  );
  assert.equal(statusIsReady(status, false), false);
});

test("请求日志会脱敏 HTTP 与 WebSocket caller capability", async () => {
  const router = await startRouter({ CODEX_ROUTER_REQUEST_LOG: "1" });
  let socket;
  try {
    const response = await fetch(
      `http://127.0.0.1:${router.routerPort}/_codex-router/${callerKey}/v1/models`,
    );
    assert.equal(response.status, 401);
    await response.arrayBuffer();

    socket = net.createConnection({ host: "127.0.0.1", port: router.routerPort });
    await once(socket, "connect");
    socket.write(
      `GET /_codex-router/${callerKey}/v1/realtime HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: Upgrade\r\nUpgrade: websocket\r\n\r\n`,
    );
    socket.resume();
    await once(socket, "close");
    for (let attempt = 0; attempt < 20 && !router.stderr().includes("WS upgrade"); attempt += 1) {
      await new Promise((resolve) => setTimeout(resolve, 25));
    }
    const logs = router.stderr();
    assert.equal(logs.includes(callerKey), false);
    assert.match(logs, /\[REDACTED\]/);
    assert.match(logs, /WS upgrade .*\[REDACTED\]/);
  } finally {
    socket?.destroy();
    await router.close();
  }
});

test("上游巨型错误只读取有界内容并脱敏", async () => {
  const secret = "super-secret-upstream-token";
  const body = JSON.stringify({
    error: {
      message: `Bearer ${secret}; token=${secret}; insufficient_quota\u0001`,
    },
  }) + "x".repeat(200_000);
  const upstream = http.createServer((_request, response) => {
    response.writeHead(401, { "Content-Type": "application/json" });
    response.end(body);
  });
  const upstreamPort = await listen(upstream);
  const router = await startRouter({ MIMO_BASE_URL: `http://127.0.0.1:${upstreamPort}` });
  try {
    const response = await fetch(
      `http://127.0.0.1:${router.routerPort}/_codex-router/${callerKey}/v1/responses`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          model: "mimo-token-plan/mimo-v2.5-pro",
          input: "test",
          stream: false,
        }),
      },
    );
    const output = await response.text();
    assert.equal(response.status, 401);
    assert.match(output, /billing_error/);
    assert.equal(output.includes(secret), false);
    assert.equal(output.includes("x".repeat(1_000)), false);
    assert.equal(output.includes("\u0001"), false);
    assert.ok(output.length < 2_000);
  } finally {
    await router.close();
    await close(upstream);
  }
});

test("静默 SSE 在 idle timeout 后发送 terminal error", async () => {
  const upstream = http.createServer((_request, response) => {
    response.writeHead(200);
    response.flushHeaders();
  });
  const upstreamPort = await listen(upstream);
  const router = await startRouter({
    MIMO_BASE_URL: `http://127.0.0.1:${upstreamPort}`,
    CODEX_ROUTER_STREAM_IDLE_TIMEOUT_MS: "100",
  });
  try {
    const response = await fetch(
      `http://127.0.0.1:${router.routerPort}/_codex-router/${callerKey}/v1/responses`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          model: "mimo-token-plan/mimo-v2.5-pro",
          input: "test",
          stream: true,
        }),
      },
    );
    const output = await response.text();
    assert.equal(response.status, 200);
    assert.match(response.headers.get("content-type"), /text\/event-stream/);
    assert.match(output, /local_router_stream_failed/);
    assert.match(output, /event: error/);
  } finally {
    await router.close();
    await close(upstream);
  }
});

test("非 SSE 在首字节 idle timeout 后返回 504 JSON", async () => {
  const upstream = http.createServer((_request, response) => {
    response.writeHead(200, { "Content-Type": "application/json" });
    response.flushHeaders();
  });
  const upstreamPort = await listen(upstream);
  const router = await startRouter({
    MIMO_BASE_URL: `http://127.0.0.1:${upstreamPort}`,
    CODEX_ROUTER_STREAM_IDLE_TIMEOUT_MS: "100",
  });
  try {
    const response = await fetch(
      `http://127.0.0.1:${router.routerPort}/_codex-router/${callerKey}/v1/responses`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          model: "mimo-token-plan/mimo-v2.5-pro",
          input: "test",
          stream: false,
        }),
      },
    );
    assert.equal(response.status, 504);
    assert.deepEqual(await response.json(), {
      error: {
        type: "local_router_stream_failed",
        message: "The upstream response stream stopped producing data.",
      },
    });
  } finally {
    await router.close();
    await close(upstream);
  }
});

test("持续 SSE chunk 会重置 idle timeout", async () => {
  const upstream = http.createServer((_request, response) => {
    response.writeHead(200, { "Content-Type": "text/event-stream" });
    let count = 0;
    const timer = setInterval(() => {
      response.write(`data: ${count++}\n\n`);
      if (count === 5) {
        clearInterval(timer);
        response.end();
      }
    }, 75);
  });
  const upstreamPort = await listen(upstream);
  const router = await startRouter({
    MIMO_BASE_URL: `http://127.0.0.1:${upstreamPort}`,
    CODEX_ROUTER_STREAM_IDLE_TIMEOUT_MS: "300",
  });
  try {
    const response = await fetch(
      `http://127.0.0.1:${router.routerPort}/_codex-router/${callerKey}/v1/responses`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          model: "mimo-token-plan/mimo-v2.5-pro",
          input: "test",
          stream: true,
        }),
      },
    );
    const output = await response.text();
    assert.equal(response.status, 200);
    assert.match(output, /data: 0/);
    assert.match(output, /data: 4/);
    assert.equal(output.includes("local_router_stream_failed"), false);
  } finally {
    await router.close();
    await close(upstream);
  }
});


test("WLB 明确额度耗尽在输出前持久化官方并等待，原生身份和压缩协议保持完整", async () => {
  const received = [];
  let rejection = { status: 403, error: { message: "当前拼车 Key 每日额度已经用完，请等待次日额度重置" } };
  let officialStatus = 200;
  let sabotage;
  const official = http.createServer(async (request, response) => {
    const chunks = [];
    for await (const chunk of request) chunks.push(chunk);
    let bytes = Buffer.concat(chunks);
    if (request.headers["content-encoding"] === "zstd") bytes = zstdDecompressSync(bytes);
    received.push({ provider: "official", url: request.url, headers: request.headers, body: JSON.parse(bytes.toString()), at: Date.now() });
    response.writeHead(officialStatus, { "Content-Type": "application/json" });
    response.end(JSON.stringify(officialStatus === 200 ? { output: [], usage: { input_tokens: 7, output_tokens: 2 } } : { error: { message: "official failure" } }));
  });
  const wlb = http.createServer(async (request, response) => {
    const chunks = [];
    for await (const chunk of request) chunks.push(chunk);
    received.push({ provider: "wlb", body: JSON.parse(Buffer.concat(chunks).toString()), headers: request.headers, at: Date.now() });
    sabotage?.();
    if (rejection.stream) {
      response.writeHead(200, { "Content-Type": "text/event-stream" });
      response.write('data: {"type":"response.created"}\n\n');
      response.end(`data: ${JSON.stringify({ type: "error", error: { message: "额度已用完" } })}\n\n`);
      return;
    }
    response.writeHead(rejection.status, { "Content-Type": rejection.text ? "text/plain" : "application/json" });
    response.end(rejection.text || JSON.stringify({ error: rejection.error }));
  });
  const officialPort = await listen(official);
  const wlbPort = await listen(wlb);
  const router = await startRouter({ CODEX_NATIVE_BASE_URL: `http://127.0.0.1:${officialPort}`, WLB_BASE_URL: `http://127.0.0.1:${wlbPort}`, WLB_API_KEY: "test-wlb-key", CODEX_ROUTER_WLB_FALLBACK_DELAY_MS: "80", CODEX_ROUTER_NATIVE_RETRIES: "0" });
  const statePath = path.join(router.stateDir, "gpt-route.json");
  const reset = () => writeFileSync(statePath, JSON.stringify({ version: 1, route: "wlb" }));
  const payload = { model: "gpt-5.6-sol", stream: false, client_metadata: { retained: true }, prompt_cache_retention: "24h", previous_response_id: "old", tools: [{ type: "function", name: "test", parameters: {} }], input: [{ role: "user", content: "hello" }, { type: "reasoning", encrypted_content: "other-provider", content: [] }] };
  const send = async (endpoint = "/responses", body = payload, headers = {}, signal) => {
    const result = await fetch(`http://127.0.0.1:${router.routerPort}/_codex-router/${callerKey}${endpoint}`, {
      method: "POST", headers: { "Content-Type": "application/json", Authorization: "Bearer original-official", "chatgpt-account-id": "original-account", ...headers }, body: JSON.stringify(body), signal,
    });
    return { status: result.status, body: await result.json() };
  };
  try {
    for (const endpoint of ["/responses", "/v1/responses", "/responses/compact", "/v1/responses/compact"]) {
      reset();
      const before = received.length;
      assert.equal((await send(endpoint)).status, 200);
      const [rejected, retried] = received.slice(before);
      assert.equal(rejected.provider, "wlb");
      assert.equal(rejected.headers.authorization, "Bearer test-wlb-key");
      assert.equal(retried.provider, "official");
      assert.equal(retried.url, endpoint.replace(/^\/v1/, ""));
      assert.ok(retried.at - rejected.at >= 65, "fallback must wait");
      assert.equal(retried.headers.authorization, "Bearer original-official");
      assert.equal(retried.headers["chatgpt-account-id"], "original-account");
      assert.deepEqual(retried.body.tools, payload.tools);
      assert.deepEqual(retried.body.client_metadata, payload.client_metadata);
      assert.equal(retried.body.model, payload.model);
      assert.equal(retried.body.prompt_cache_retention, undefined);
      assert.equal(retried.body.input.length, 1);
      assert.equal(retried.body.previous_response_id, endpoint.endsWith("compact") ? "old" : undefined);
      assert.equal(JSON.parse(readFileSync(statePath)).route, "official");
      assert.equal((await send()).status, 200);
      assert.equal(received.at(-1).provider, "official");
    }
    for (const endpoint of ["/responses", "/v1/responses"]) {
      reset();
      rejection = { status: 403, error: { message: "当前拼车 Key 每日额度已经用完，请等待次日额度重置" } };
      const input = [...payload.input, { type: "compaction_trigger" }];
      const before = received.length;
      assert.equal((await send(endpoint, { ...payload, input })).status, 200);
      assert.equal(received.length - before, 2, "one summary rejection and one original official request");
      assert.deepEqual(received.at(-1).body.input.at(-1), { type: "compaction_trigger" });
      assert.deepEqual(received.at(-1).body.tools, payload.tools);
    }
    reset();
    rejection = { status: 403, text: "日限额额度已用完" };
    assert.equal((await send("/responses/compact")).status, 200);
    assert.equal(received.at(-1).provider, "official");
    reset();
    rejection = { status: 403, error: { message: "quota exhausted; upgrade your plan" } };
    assert.equal((await send("/responses", { ...payload, input: [{ role: "user", content: "large history ".repeat(3000) }] })).status, 200);
    assert.equal(received.at(-1).headers["content-encoding"], "zstd");
    assert.equal(received.at(-1).body.input[0].content, "large history ".repeat(3000));
    reset();
    rejection = { stream: true };
    const beforeStream = received.length;
    const streamResponse = await fetch(`http://127.0.0.1:${router.routerPort}/_codex-router/${callerKey}/responses`, {
      method: "POST", headers: { "Content-Type": "application/json", Authorization: "Bearer original-official" }, body: JSON.stringify({ ...payload, stream: true }),
    });
    assert.equal(streamResponse.status, 200);
    assert.match(await streamResponse.text(), /额度已用完/);
    assert.equal(received.length - beforeStream, 1);
    assert.equal(JSON.parse(readFileSync(statePath)).route, "wlb");
    for (const error of [
      { status: 403, error: { message: "invalid API key" } },
      { status: 429, error: { type: "rate_limit_error", message: "too many requests" } },
      { status: 429, error: { type: "resource_exhausted", message: "rate limit" } },
      { status: 429, error: { code: "quota_exceeded", message: "Per-minute request quota exceeded. Retry in 60 seconds." } },
      { status: 429, error: { type: "rate_limit_error", message: "Request quota exceeded, retry after 10 seconds." } },
      { status: 429, error: { type: "rate_limit_error", message: "Your daily usage limit is 1000 requests, try again later." } },
      { status: 404, error: { message: "quota exhausted model not found" } },
      { status: 403, error: { type: "insufficient_quota", message: "Your plan does not include API access" } },
    ]) {
      reset(); rejection = error;
      const before = received.length;
      assert.equal((await send()).status, error.status);
      assert.equal(received.length - before, 1);
      assert.equal(JSON.parse(readFileSync(statePath)).route, "wlb");
    }
    rejection = { status: 403, error: { message: "额度已用完" } };
    for (const headers of [{ Authorization: "" }, { "x-openai-subagent": "true" }]) {
      reset();
      const before = received.length;
      await send("/responses", payload, headers);
      assert.equal(received.length - before, 1);
      assert.equal(JSON.parse(readFileSync(statePath)).route, "wlb");
    }
    reset();
    const beforeExplicit = received.length;
    assert.equal((await send("/responses", { ...payload, model: "wlb-relay/gpt-5.6-sol" })).status, 403);
    assert.equal(received.length - beforeExplicit, 1);
    assert.equal(JSON.parse(readFileSync(statePath)).route, "wlb");
    reset(); officialStatus = 403;
    const beforeOfficialError = received.length;
    assert.deepEqual(await send(), { status: 403, body: { error: { message: "official failure" } } });
    assert.equal(received.length - beforeOfficialError, 2);
    officialStatus = 200;
    reset();
    sabotage = () => writeFileSync(statePath, "broken");
    const beforeBroken = received.length;
    assert.equal((await send()).status, 503);
    assert.equal(received.length - beforeBroken, 1);
    sabotage = undefined;
    reset();
    mkdirSync(`${statePath}.${router.router.pid}.tmp`);
    const beforeWriteFailure = received.length;
    assert.equal((await send()).status, 503);
    assert.equal(received.length - beforeWriteFailure, 1);
    assert.equal(JSON.parse(readFileSync(statePath)).route, "wlb");
    rmSync(`${statePath}.${router.router.pid}.tmp`, { recursive: true });
    reset();
    const abort = new AbortController();
    const beforeAbort = received.length;
    const pending = send("/responses", payload, {}, abort.signal);
    await waitUntil(() => JSON.parse(readFileSync(statePath)).route === "official", "fallback did not save state");
    abort.abort();
    await assert.rejects(pending, { name: "AbortError" });
    await new Promise((resolve) => setTimeout(resolve, 120));
    assert.equal(received.length - beforeAbort, 1);
    const events = usageEvents(router.stateDir);
    assert.ok(events.some((event) => event.provider === "wlb-relay" && event.status === 403));
    assert.ok(events.some((event) => event.provider === "openai" && event.inputTokens === 7));
  } finally {
    await router.close();
    await close(official);
    await close(wlb);
  }
});


test("自动切换只识别明确额度耗尽，套餐营销和普通 resource_exhausted 不触发", () => {
  for (const message of ["日限额额度已用完", "当前拼车 Key 每日额度已经用完，请等待次日额度重置", "日额度已耗尽", "每日额度已用尽", "quota exhausted; upgrade your plan", "余额不足"]) {
    assert.equal(isUsageExhausted(403, JSON.stringify({ error: { message } })), true);
  }
  for (const message of ["upgrade your plan", "purchase extra usage", "Your plan does not include API access; quota exhausted", "too many requests"]) {
    assert.equal(isUsageExhausted(429, JSON.stringify({ error: { message, type: "resource_exhausted" } })), false);
  }
  assert.equal(isUsageExhausted(429, JSON.stringify({ error: { code: "insufficient_quota", message: "limit" } })), true);
  assert.equal(isUsageExhausted(503, "额度已用完"), false);
});


test("WLB 自动切换区分临时请求配额和账单硬额度，并保留升级建议后的真实额度错误", () => {
  const cases = [
    [{ code: "quota_exceeded", message: "Per-minute request quota exceeded. Retry in 60 seconds." }, false],
    [{ type: "rate_limit_error", message: "Request quota exceeded, retry after 10 seconds." }, false],
    [{ type: "rate_limit_error", message: "Your daily usage limit is 1000 requests, try again later." }, false],
    [{ code: "quota_exceeded", message: "limit" }, false],
    [{ code: "quota_exhausted", message: "Token quota exhausted" }, false],
    [{ message: "Quota exhausted for requests this hour" }, false],
    [{ message: "usage limit reached" }, false],
    [{ message: "reached your daily limit" }, false],
    [{ code: "insufficient_quota", message: "You exceeded your current quota. Upgrade to Pro plan." }, true],
    [{ code: "billing_hard_limit_reached", message: "Upgrade to Pro plan." }, true],
    [{ message: "Your plan does not include API access", code: "insufficient_quota" }, false],
    [{ message: "余额不足，请升级套餐" }, true],
  ];
  for (const [error, expected] of cases) {
    assert.equal(isUsageExhausted(429, JSON.stringify({ error })), expected, JSON.stringify(error));
  }
});


test("SuperGPT 原名路由隔离凭据，支持 JSON/SSE/压缩，子代理和模型目录保留官方", async () => {
  const received = [];
  const catalog = { models: [{ slug: "gpt-future", future_capability: true }] };
  const sse = 'event: response.completed\ndata: {"type":"response.completed","response":{"usage":{"input_tokens":4,"output_tokens":2}}}\n\n';
  const upstream = (provider) => http.createServer(async (request, response) => {
    const chunks = [];
    for await (const chunk of request) chunks.push(chunk);
    const body = chunks.length ? JSON.parse(Buffer.concat(chunks).toString()) : undefined;
    received.push({ provider, url: request.url, headers: request.headers, body });
    if (request.method === "GET") {
      response.writeHead(200, { "Content-Type": "application/json" });
      response.end(JSON.stringify(catalog));
    } else if (body.model === "gpt-no-quota") {
      response.writeHead(429, { "Content-Type": "application/json" });
      response.end(JSON.stringify({ error: { code: "insufficient_quota", message: "Insufficient balance" } }));
    } else if (body.model === "gpt-unsupported") {
      response.writeHead(404, { "Content-Type": "application/json" });
      response.end(JSON.stringify({ error: { message: "Model not supported" } }));
    } else if (body.stream) {
      response.writeHead(200, { "Content-Type": "text/event-stream" });
      response.end(sse);
    } else {
      response.writeHead(200, { "Content-Type": "application/json" });
      response.end(JSON.stringify({ output: [{ type: "message", role: "assistant", content: [{ type: "output_text", text: "摘要" }] }] }));
    }
  });
  const official = upstream("official");
  const supergpt = upstream("supergpt");
  const officialPort = await listen(official);
  const supergptPort = await listen(supergpt);
  const router = await startRouter({
    CODEX_NATIVE_BASE_URL: `http://127.0.0.1:${officialPort}`,
    SUPERGPT_BASE_URL: `http://127.0.0.1:${supergptPort}/v1/`,
    SUPERGPT_API_KEY: "test-supergpt-key",
    CODEX_ROUTER_SESSIONS_PATH: "", CODEX_ROUTER_SESSION_INDEX: "",
  });
  const statePath = path.join(router.stateDir, "gpt-route.json");
  const selectionPath = path.join(router.stateDir, "enabled-providers.json");
  const cli = (command, extraEnv = {}) => spawnSync(process.execPath, [path.join(root, "src/gpt-route.mjs"), command], {
    encoding: "utf8", env: { ...process.env, CODEX_HOME: router.stateDir, MODEL_ROUTER_STATE_DIR: router.stateDir, SUPERGPT_API_KEY: "test-supergpt-key", ...extraEnv },
  });
  const send = (model, { endpoint = "responses", stream = false, headers = {}, input = [{ role: "user", content: "你好" }] } = {}) => fetch(
    `http://127.0.0.1:${router.routerPort}/_codex-router/${callerKey}/v1/${endpoint}`, {
      method: "POST", headers: { "Content-Type": "application/json", Authorization: "Bearer test-official-key", "ChatGPT-Account-Id": "private-account", Cookie: "private-session", ...headers },
      body: JSON.stringify({ model, input, stream }),
    },
  );
  try {
    const missingKey = cli("supergpt", { SUPERGPT_API_KEY: "" });
    assert.equal(missingKey.status, 1);
    assert.match(missingKey.stderr, /SuperGPT 未配置密钥/);
    assert.equal(existsSync(statePath), false);
    writeFileSync(selectionPath, JSON.stringify({ version: 1, providers: ["wlb-relay"] }));
    assert.match(cli("supergpt").stderr, /SuperGPT 尚未启用/);
    assert.equal(existsSync(statePath), false);
    writeFileSync(selectionPath, JSON.stringify({ version: 1, providers: ["supergpt-relay"] }));
    const switched = cli("supergpt");
    assert.equal(switched.status, 0, switched.stderr);
    const before = readFileSync(statePath, "utf8");
    assert.match(cli("status").stdout, /SuperGPT \(supergpt\)/);
    assert.equal(readFileSync(statePath, "utf8"), before);
    for (const model of ["gpt-5.6-sol", "gpt-6.1-sol", "gpt-future"]) {
      const response = await send(model);
      assert.equal(response.status, 200);
      await response.json();
      const last = received.at(-1);
      assert.equal(last.provider, "supergpt");
      assert.equal(last.url, "/v1/responses");
      assert.equal(last.body.model, model);
      assert.equal(last.headers.authorization, "Bearer test-supergpt-key");
      assert.equal(last.headers["chatgpt-account-id"], undefined);
      assert.equal(last.headers.cookie, undefined);
    }
    const streamed = await send("gpt-future", { stream: true });
    assert.equal(streamed.status, 200);
    assert.equal(await streamed.text(), sse);
    for (const options of [{ endpoint: "responses/compact" }, { input: [{ role: "user", content: "你好" }, { type: "compaction_trigger" }] }]) {
      const compact = await send("gpt-future", options);
      assert.equal(compact.status, 200);
      await compact.json();
      assert.equal(received.at(-1).provider, "supergpt");
      assert.equal(received.at(-1).url, "/v1/responses");
      assert.equal(received.at(-1).body.model, "gpt-future");
    }
    const child = await send("gpt-future", { headers: { "x-openai-subagent": "true" } });
    assert.equal(child.status, 200);
    await child.json();
    assert.equal(received.at(-1).provider, "official");
    assert.equal(received.at(-1).headers.authorization, "Bearer test-official-key");
    for (const endpoint of ["models", "v1/models"]) {
      const models = await fetch(`http://127.0.0.1:${router.routerPort}/_codex-router/${callerKey}/${endpoint}?client_version=supergpt-test`, { headers: { Authorization: "Bearer test-official-key" } });
      assert.deepEqual(await models.json(), catalog);
      assert.equal(received.at(-1).provider, "official");
      assert.equal(received.at(-1).url, "/models?client_version=supergpt-test");
    }
    for (const [model, status] of [["gpt-no-quota", 429], ["gpt-unsupported", 404]]) {
      const count = received.length;
      const response = await send(model);
      assert.equal(response.status, status);
      await response.json();
      assert.equal(received.length, count + 1);
      assert.equal(readFileSync(statePath, "utf8"), before);
    }
    writeFileSync(selectionPath, JSON.stringify({ version: 1, providers: [] }));
    const disabled = await send("gpt-future");
    assert.equal(disabled.status, 409);
    assert.equal((await disabled.json()).error.type, "provider_not_enabled");
    assert.equal(cli("official").status, 0);
  } finally {
    await router.close();
    await close(official);
    await close(supergpt);
  }
});


test("审计：失败或空摘要不能替换压缩历史", async () => {
  let result;
  const upstream = http.createServer(async (request, response) => {
    for await (const _ of request) {}
    response.writeHead(200, { "Content-Type": "application/json" });
    response.end(JSON.stringify(result));
  });
  const port = await listen(upstream);
  const router = await startRouter({ SUPERGPT_BASE_URL: `http://127.0.0.1:${port}`, SUPERGPT_API_KEY: "test-key" });
  writeFileSync(path.join(router.stateDir, "gpt-route.json"), JSON.stringify({ version: 1, route: "supergpt" }));
  try {
    const original = [{ role: "user", content: "continue" }, { type: "function_call_output", call_id: "call_test", output: "critical intermediate result" }];
    for (const failed of [
      { status: "failed", error: { message: "failure" }, output: [] },
      { status: "incomplete", output_text: "partial summary" },
      { status: "in_progress", output_text: "unfinished summary" },
      { status: "completed", output: [] },
      { status: "completed", output_text: "   " },
      { error: { message: "failure without status" } },
    ]) {
      result = failed;
      for (const v2 of [false, true]) {
        const response = await fetch(`http://127.0.0.1:${router.routerPort}/_codex-router/${callerKey}/responses${v2 ? "" : "/compact"}`, {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ model: "gpt-test", stream: false, input: v2 ? [...original, { type: "compaction_trigger" }] : original }),
        });
        assert.equal(response.status, 502, JSON.stringify(failed));
        const body = await response.json();
        assert.ok(body.error);
        assert.equal(body.output, undefined, "failed compaction must not supply replacement history");
      }
    }
    result = { output_text: "valid summary" };
    const valid = await fetch(`http://127.0.0.1:${router.routerPort}/_codex-router/${callerKey}/responses/compact`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ model: "gpt-test", input: original }),
    });
    assert.equal(valid.status, 200, "providers omitting status remain compatible");
    assert.match(JSON.stringify(await valid.json()), /valid summary/);
  } finally { await router.close(); await close(upstream); }
});

test("审计：压缩错误和普通错误一致脱敏并保留 Retry-After", async () => {
  const secret = "sk-audit-placeholder-00000000000000000000";
  const upstream = http.createServer(async (request, response) => {
    for await (const _ of request) {}
    response.writeHead(429, { "Content-Type": "application/json", "Retry-After": "11" });
    response.end(JSON.stringify({ error: { message: `rate limit; Authorization: Bearer ${secret}`, api_key: secret } }));
  });
  const port = await listen(upstream);
  const router = await startRouter({ SUPERGPT_BASE_URL: `http://127.0.0.1:${port}`, SUPERGPT_API_KEY: "test-key" });
  writeFileSync(path.join(router.stateDir, "gpt-route.json"), JSON.stringify({ version: 1, route: "supergpt" }));
  try {
    for (const mode of ["normal", "compact", "v2"]) {
      const input = [{ role: "user", content: "test" }, ...(mode === "v2" ? [{ type: "compaction_trigger" }] : [])];
      const response = await fetch(`http://127.0.0.1:${router.routerPort}/_codex-router/${callerKey}/responses${mode === "compact" ? "/compact" : ""}`, {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ model: "gpt-test", input, stream: false }),
      });
      const body = await response.text();
      assert.equal(response.status, 429);
      assert.equal(body.includes(secret), false, mode);
      assert.equal(response.headers.get("retry-after"), "11", mode);
      assert.ok(JSON.parse(body).error);
    }
  } finally { await router.close(); await close(upstream); }
});

test("审计：旧 WLB 请求不能覆盖之后的手动切换或同路由重选", async () => {
  let rejectWlb;
  let officialRequests = 0;
  const wlb = http.createServer(async (request, response) => {
    for await (const _ of request) {}
    rejectWlb = () => { response.writeHead(429, { "Content-Type": "application/json" }); response.end(JSON.stringify({ error: { code: "insufficient_quota", message: "quota exhausted" } })); };
  });
  const official = http.createServer((request, response) => {
    assert.equal(request.headers.authorization, "Bearer test-official");
    assert.equal(request.headers["chatgpt-account-id"], "test-account");
    officialRequests += 1; request.resume(); response.writeHead(200, { "Content-Type": "application/json" }); response.end('{"output":[]}');
  });
  const wlbPort = await listen(wlb);
  const officialPort = await listen(official);
  const router = await startRouter({ WLB_BASE_URL: `http://127.0.0.1:${wlbPort}`, WLB_API_KEY: "test-wlb-key", SUPERGPT_API_KEY: "test-supergpt-key", CODEX_NATIVE_BASE_URL: `http://127.0.0.1:${officialPort}`, CODEX_ROUTER_WLB_FALLBACK_DELAY_MS: "0" });
  const statePath = path.join(router.stateDir, "gpt-route.json");
  const cli = (route) => {
    const result = spawnSync(process.execPath, [path.join(root, "src/gpt-route.mjs"), route], { encoding: "utf8", windowsHide: true, env: { ...process.env, CODEX_HOME: router.stateDir, MODEL_ROUTER_STATE_DIR: router.stateDir, WLB_API_KEY: "test-wlb-key", SUPERGPT_API_KEY: "test-supergpt-key" } });
    assert.equal(result.status, 0, result.stderr);
  };
  try {
    for (const mode of ["normal", "compact", "v2"]) {
      const endpoint = mode === "compact" ? "responses/compact" : "responses";
      for (const choice of ["supergpt", "wlb", "official"]) {
        cli("wlb"); rejectWlb = undefined;
        const beforeOfficial = officialRequests;
        const pending = fetch(`http://127.0.0.1:${router.routerPort}/_codex-router/${callerKey}/${endpoint}`, {
          method: "POST", headers: { "Content-Type": "application/json", Authorization: "Bearer test-official", "chatgpt-account-id": "test-account" },
          body: JSON.stringify({ model: "gpt-test", input: mode === "v2" ? [{ type: "compaction_trigger" }] : [], stream: false }),
        });
        await waitUntil(() => rejectWlb, "WLB did not receive the request");
        cli(choice);
        const chosenState = readFileSync(statePath, "utf8");
        rejectWlb();
        const response = await pending;
        await response.text();
        assert.equal(readFileSync(statePath, "utf8"), chosenState, `old ${mode} overwrote ${choice}`);
        assert.equal(response.status, choice === "official" ? 200 : 429, `${mode}: ${choice}`);
        assert.equal(officialRequests - beforeOfficial, choice === "official" ? 1 : 0, `${mode}: ${choice}`);
      }
    }
  } finally { await router.close(); await close(wlb); await close(official); }
});

test("审计：错误体和压缩读取遵循闲置超时", async () => {
  const upstream = http.createServer(async (request, response) => {
    const chunks = []; for await (const chunk of request) chunks.push(chunk);
    const payload = JSON.parse(Buffer.concat(chunks));
    response.writeHead(payload.model === "gpt-error" ? 400 : 200, { "Content-Type": "application/json" });
    response.write('{"partial":');
  });
  const port = await listen(upstream);
  const router = await startRouter({ SUPERGPT_BASE_URL: `http://127.0.0.1:${port}`, SUPERGPT_API_KEY: "test-key", CODEX_ROUTER_STREAM_IDLE_TIMEOUT_MS: "35" });
  writeFileSync(path.join(router.stateDir, "gpt-route.json"), JSON.stringify({ version: 1, route: "supergpt" }));
  try {
    for (const [model, endpoint] of [["gpt-error", "responses"], ["gpt-ok", "responses/compact"]]) {
      const response = await fetch(`http://127.0.0.1:${router.routerPort}/_codex-router/${callerKey}/${endpoint}`, {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ model, input: [] }), signal: AbortSignal.timeout(1500),
      });
      assert.equal(response.status, 504);
      assert.ok((await response.json()).error);
    }
  } finally { await router.close(); upstream.closeAllConnections(); await close(upstream); }
});


test("审计：Lite 拒绝已退役的非 Responses 协议", () => {
  const directory = mkdtempSync(path.join(os.tmpdir(), "codex-router-protocol-"));
  const registryPath = path.join(directory, "registry.json");
  const provider = JSON.parse(readFileSync(path.join(root, "config/supergpt/supergpt.json"))).providers[0];
  try {
    for (const protocol of [undefined, "openai", "anthropic", "openai-responses"]) {
      writeFileSync(registryPath, JSON.stringify({ version: 1, providers: [{ ...provider, protocol }], models: [] }));
      const result = spawnSync(process.execPath, ["--input-type=module", "-e", "await import('./src/model-registry.mjs')"], {
        cwd: root, encoding: "utf8", windowsHide: true,
        env: { ...process.env, MODEL_ROUTER_REGISTRY: registryPath },
      });
      assert.equal(result.status, protocol === "openai-responses" ? 0 : 1, String(protocol));
      if (protocol !== "openai-responses") assert.match(result.stderr, /openai-responses/);
    }
  } finally { rmSync(directory, { recursive: true, force: true }); }
});


test("WLB 自动回退默认立即重发且继续保存官方路由", async () => {
  const received = [];
  const official = http.createServer((request, response) => {
    received.push("official"); request.resume();
    response.writeHead(200, { "Content-Type": "application/json" });
    response.end(JSON.stringify({ output: [] }));
  });
  const wlb = http.createServer((request, response) => {
    received.push("wlb"); request.resume();
    response.writeHead(429, { "Content-Type": "application/json" });
    response.end(JSON.stringify({ error: { code: "insufficient_quota", message: "quota exhausted" } }));
  });
  const officialPort = await listen(official);
  const wlbPort = await listen(wlb);
  const router = await startRouter({
    CODEX_NATIVE_BASE_URL: `http://127.0.0.1:${officialPort}`,
    WLB_BASE_URL: `http://127.0.0.1:${wlbPort}`,
    WLB_API_KEY: "test-wlb-key",
    CODEX_ROUTER_WLB_FALLBACK_DELAY_MS: undefined,
  });
  const statePath = path.join(router.stateDir, "gpt-route.json");
  writeFileSync(statePath, JSON.stringify({ version: 1, route: "wlb" }));
  try {
    const response = await fetch(`http://127.0.0.1:${router.routerPort}/_codex-router/${callerKey}/responses`, {
      method: "POST",
      headers: { "Content-Type": "application/json", Authorization: "Bearer test-official" },
      body: JSON.stringify({ model: "gpt-test", input: [], stream: false }),
    });
    assert.equal(response.status, 200);
    await response.json();
    assert.deepEqual(received, ["wlb", "official"]);
    assert.equal(JSON.parse(readFileSync(statePath)).route, "official");
    assert.match(router.stderr(), /route=official wait-ms=0(?:\r?\n|$)/);
  } finally { await router.close(); await close(wlb); await close(official); }
});


test("auto 主代理按额度阈值恢复优先级，普通和两种压缩完整降级且保持身份隔离", async () => {
  const seen = [];
  let daily = 10;
  let weekly = 20;
  let fiveHour = 80;
  let gptWeekly = 80;
  let reject = new Set();
  let wlbStatus = 403;
  let wlbMessage = "每日额度已经用完";
  let sse = false;
  let holdWlb = false;
  let releaseWlb;
  let wlbArrived = false;
  let nativeError;
  const upstream = (provider) => http.createServer(async (request, response) => {
    if (request.method === "GET") {
      if (provider === "wlb") {
        assert.equal(request.headers.authorization, "Bearer test-wlb-key");
        assert.equal(request.headers["chatgpt-account-id"], undefined);
        response.end(JSON.stringify({ rate_limits: [{ window: "1d", remaining: daily }, { window: "7d", remaining: weekly }] }));
      } else {
        assert.equal(request.headers.authorization, "Bearer auto-official");
        assert.equal(request.headers["chatgpt-account-id"], "auto-account");
        response.end(JSON.stringify({ rate_limit: { ...(fiveHour === null ? {} : { primary_window: { used_percent: 100 - fiveHour } }), secondary_window: { used_percent: 100 - gptWeekly } } }));
      }
      return;
    }
    const chunks = []; for await (const chunk of request) chunks.push(chunk);
    let bytes = Buffer.concat(chunks);
    if (request.headers["content-encoding"] === "zstd") bytes = zstdDecompressSync(bytes);
    const body = JSON.parse(bytes);
    seen.push({ provider, url: request.url, body });
    assert.equal(request.headers.authorization, provider === "official" ? "Bearer auto-official" : `Bearer test-${provider}-key`);
    assert.equal(request.headers["chatgpt-account-id"], provider === "official" ? "auto-account" : undefined);
    if (provider !== "official") assert.equal(request.url, "/responses");
    if (provider === "wlb" && holdWlb) {
      await new Promise((resolve) => { releaseWlb = resolve; wlbArrived = true; });
    }
    if (provider === "official" && nativeError) {
      response.writeHead(429, { "Content-Type": "application/octet-stream", "Retry-After": "9", "X-Original-Error": "native" });
      response.end(nativeError);
      return;
    }
    if (sse && provider === "wlb") {
      response.writeHead(200, { "Content-Type": "text/event-stream" });
      response.end('data: {"type":"error","error":{"message":"额度已用完"}}\n\n');
      return;
    }
    if (reject.has(provider)) {
      response.writeHead(provider === "wlb" ? wlbStatus : 429, { "Content-Type": "application/json", "Retry-After": "7", "X-Original-Error": "retained" });
      response.end(JSON.stringify({ error: provider === "official" ? { code: "usage_limit_reached", message: "usage limit" } : { message: wlbMessage } }));
      return;
    }
    response.writeHead(200, { "Content-Type": "application/json" });
    response.end(JSON.stringify({ output: [{ type: "message", role: "assistant", content: [{ type: "output_text", text: "摘要" }] }], usage: { input_tokens: 3, output_tokens: 2 } }));
  });
  const servers = [upstream("wlb"), upstream("official"), upstream("supergpt")];
  const ports = await Promise.all(servers.map(listen));
  const router = await startRouter({ WLB_BASE_URL: `http://127.0.0.1:${ports[0]}`, WLB_API_KEY: "test-wlb-key", CODEX_NATIVE_BASE_URL: `http://127.0.0.1:${ports[1]}`, CODEX_ROUTER_GPT_USAGE_URL: `http://127.0.0.1:${ports[1]}/usage`, SUPERGPT_BASE_URL: `http://127.0.0.1:${ports[2]}`, SUPERGPT_API_KEY: "test-supergpt-key", CODEX_ROUTER_AUTO_REFRESH_MS: "0" });
  const statePath = path.join(router.stateDir, "gpt-route.json");
  const configured = JSON.stringify({ version: 1, route: "auto", revision: "test-auto-revision" });
  writeFileSync(statePath, configured);
  const send = async (mode = "normal", extraHeaders = {}, model = "gpt-auto-test") => {
    const result = await fetch(`http://127.0.0.1:${router.routerPort}/_codex-router/${callerKey}/responses${mode === "compact" ? "/compact" : ""}`, {
      method: "POST", headers: { "Content-Type": "application/json", Authorization: "Bearer auto-official", "chatgpt-account-id": "auto-account", ...extraHeaders },
      body: JSON.stringify({ model, stream: false, client_metadata: { retained: true }, previous_response_id: "foreign-provider-response", tools: [{ type: "function", name: "original", parameters: {} }], input: [{ role: "user", content: "原始上下文" }, { type: "message", role: "assistant", content: [{ type: "output_text", text: "历史输出" }] }, ...(mode === "v2" ? [{ type: "compaction_trigger" }] : [])] }),
    });
    return { status: result.status, headers: result.headers, body: await result.text() };
  };
  try {
    for (const [d, w, five, week, expected] of [
      [10, 20, 80, 80, "wlb"], [0, 20, 80, 80, "official"],
      [0, 20, 80, 5, "supergpt"], [0, 20, 80, 4, "supergpt"], [0, 20, 80, 0, "supergpt"],
      [0, 20, 80, 6, "official"], [0, 20, 10, 5, "supergpt"],
      [0, 20, 80, 5, "supergpt"], [0, 20, 10, 6, "supergpt"], [0, 20, 11, 6, "official"],
      [10, 0, 5, 80, "supergpt"], [0, 20, null, 2, "supergpt"],
      [0, 20, null, 5, "supergpt"], [0, 20, null, 6, "official"], [10, 20, 80, 80, "wlb"],
    ]) {
      daily = d; weekly = w; fiveHour = five; gptWeekly = week;
      const before = seen.length;
      assert.equal((await send()).status, 200);
      assert.deepEqual(seen.slice(before).map((item) => item.provider), [expected]);
      const status = JSON.parse(readFileSync(path.join(router.stateDir, "auto-route-status.json"), "utf8"));
      assert.equal(status.snapshot.route, expected);
      assert.equal(status.configuredRevision, "test-auto-revision");
      assert.equal(JSON.stringify(status).includes("auto-official"), false);
      assert.equal(readFileSync(statePath, "utf8"), configured);
    }
    reject = new Set(["wlb", "official"]);
    for (const mode of ["normal", "compact", "v2"]) {
      const before = seen.length;
      assert.equal((await send(mode)).status, 200, mode);
      const attempts = seen.slice(before);
      assert.deepEqual(attempts.map((item) => item.provider), ["wlb", "official", "supergpt"], mode);
      assert.deepEqual(attempts[1].body.client_metadata, { retained: true });
      assert.equal(attempts[0].body.previous_response_id, undefined);
      assert.equal(attempts[1].body.previous_response_id, undefined);
      assert.equal(attempts[2].body.previous_response_id, undefined);
      assert.equal(attempts[1].body.input[0].content, "原始上下文");
      assert.equal(attempts[1].body.input[1].content[0].text, "历史输出");
      assert.equal(attempts[1].body.tools[0].name, "original");
      if (mode === "v2") assert.equal(attempts[1].body.input.at(-1).type, "compaction_trigger");
      assert.equal(readFileSync(statePath, "utf8"), configured);
    }
    reject = new Set(["wlb"]); wlbMessage = "too many requests";
    for (const status of [401, 429]) {
      wlbStatus = status;
      for (const mode of ["normal", "compact", "v2"]) {
        const before = seen.length;
        const result = await send(mode);
        assert.equal(result.status, status);
        assert.equal(result.headers.get("retry-after"), "7");
        assert.deepEqual(seen.slice(before).map((item) => item.provider), ["wlb"]);
      }
    }
    reject = new Set(); sse = true;
    const beforeSse = seen.length;
    assert.equal((await send()).status, 200);
    assert.deepEqual(seen.slice(beforeSse).map((item) => item.provider), ["wlb"]);
    sse = false;
    const beforeSubagent = seen.length;
    assert.equal((await send("normal", { "x-openai-subagent": "true" })).status, 200);
    assert.equal((await send("normal", {}, "codex-auto-review")).status, 200);
    assert.deepEqual(seen.slice(beforeSubagent).map((item) => item.provider), ["official", "official"]);
    reject = new Set(["wlb"]); wlbStatus = 403; wlbMessage = "额度已用完"; holdWlb = true;
    for (const mode of ["normal", "compact", "v2"]) {
      for (const choice of ["official", "wlb", "supergpt"]) {
        writeFileSync(statePath, configured); wlbArrived = false;
        const before = seen.length;
        const pending = send(mode);
        await waitUntil(() => wlbArrived, "auto WLB did not receive request");
        const chosen = JSON.stringify({ version: 1, route: choice, revision: `manual-${mode}-${choice}` });
        writeFileSync(statePath, chosen);
        releaseWlb();
        const result = await pending;
        assert.equal(result.status, choice === "official" ? 200 : 403, `${mode}: ${choice}`);
        assert.deepEqual(seen.slice(before).map((item) => item.provider), choice === "official" ? ["wlb", "official"] : ["wlb"]);
        assert.equal(readFileSync(statePath, "utf8"), chosen);
      }
    }
    writeFileSync(statePath, configured); wlbArrived = false;
    const abort = new AbortController();
    const beforeCancel = seen.length;
    const canceled = fetch(`http://127.0.0.1:${router.routerPort}/_codex-router/${callerKey}/responses`, {
      method: "POST", headers: { "Content-Type": "application/json", Authorization: "Bearer auto-official", "chatgpt-account-id": "auto-account" },
      body: JSON.stringify({ model: "gpt-auto-test", input: [] }), signal: abort.signal,
    });
    await waitUntil(() => wlbArrived, "auto cancel request did not reach WLB");
    abort.abort();
    await assert.rejects(canceled, { name: "AbortError" });
    releaseWlb(); holdWlb = false;
    await new Promise((resolve) => setTimeout(resolve, 80));
    assert.deepEqual(seen.slice(beforeCancel).map((item) => item.provider), ["wlb"]);
    daily = 0; nativeError = Buffer.from([0xff, 0xfe, 0x00, 0x41]);
    const beforeNativeError = seen.length;
    const nativeResponse = await fetch(`http://127.0.0.1:${router.routerPort}/_codex-router/${callerKey}/responses`, {
      method: "POST", headers: { "Content-Type": "application/json", Authorization: "Bearer auto-official", "chatgpt-account-id": "auto-account" },
      body: JSON.stringify({ model: "gpt-auto-test", input: [] }),
    });
    assert.equal(nativeResponse.status, 429);
    assert.equal(nativeResponse.headers.get("retry-after"), "9");
    assert.equal(nativeResponse.headers.get("x-original-error"), "native");
    assert.deepEqual(Buffer.from(await nativeResponse.arrayBuffer()), nativeError);
    assert.deepEqual(seen.slice(beforeNativeError).map((item) => item.provider), ["official"]);
    // 自动模式官方错误限读 64 KiB；超限明确失败，不能误判并发送到下一档。
    nativeError = Buffer.alloc(64 * 1024 + 1, 0x41);
    const beforeOversize = seen.length;
    assert.equal((await send()).status, 502);
    assert.deepEqual(seen.slice(beforeOversize).map((item) => item.provider), ["official"]);
    const events = usageEvents(router.stateDir);
    assert.ok(events.some((event) => event.provider === "openai" && event.status === 429));
    assert.ok(events.some((event) => event.provider === "supergpt-relay" && event.status === 200));
  } finally { await router.close(); await Promise.all(servers.map(close)); }
});


test("子代理 auto 仅在官方 GPT 与 SuperGPT 间切换且不请求 WLB", async () => {
  let gptUsed = 0;
  const seen = [];
  const official = http.createServer(async (request, response) => {
    if (request.method === "GET") {
      seen.push("official-usage");
      response.writeHead(200, { "Content-Type": "application/json" });
      response.end(JSON.stringify({
        rate_limit: {
          primary_window: { used_percent: gptUsed },
          secondary_window: { used_percent: 10 },
        },
      }));
      return;
    }
    seen.push("official");
    request.resume();
    response.writeHead(200, { "Content-Type": "application/json" });
    response.end(JSON.stringify({ output: [], usage: { input_tokens: 1, output_tokens: 1 } }));
  });
  const supergpt = http.createServer(async (request, response) => {
    seen.push("supergpt");
    request.resume();
    response.writeHead(200, { "Content-Type": "application/json" });
    response.end(JSON.stringify({ output: [], usage: { input_tokens: 1, output_tokens: 1 } }));
  });
  const wlb = http.createServer(async (request, response) => {
    seen.push("wlb");
    request.resume();
    response.writeHead(500, { "Content-Type": "application/json" });
    response.end("{}");
  });
  const officialPort = await listen(official);
  const supergptPort = await listen(supergpt);
  const wlbPort = await listen(wlb);
  const router = await startRouter({
    CODEX_NATIVE_BASE_URL: "http://127.0.0.1:" + officialPort,
    CODEX_ROUTER_GPT_USAGE_URL: "http://127.0.0.1:" + officialPort + "/usage",
    SUPERGPT_BASE_URL: "http://127.0.0.1:" + supergptPort + "/v1",
    SUPERGPT_API_KEY: "test-supergpt-key",
    WLB_BASE_URL: "http://127.0.0.1:" + wlbPort,
    WLB_API_KEY: "test-wlb-key",
    CODEX_ROUTER_AUTO_REFRESH_MS: "0",
  });
  const cli = (route) => spawnSync(process.execPath, [path.join(root, "src/gpt-route.mjs"), "subagent", route], {
    encoding: "utf8",
    env: {
      ...process.env,
      CODEX_HOME: router.stateDir,
      MODEL_ROUTER_STATE_DIR: router.stateDir,
      SUPERGPT_API_KEY: "test-supergpt-key",
      WLB_API_KEY: "test-wlb-key",
    },
  });
  const request = async () => {
    const response = await fetch("http://127.0.0.1:" + router.routerPort + "/_codex-router/" + callerKey + "/responses", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Authorization: "Bearer test-official-key",
        "chatgpt-account-id": "subagent-auto-account",
        "x-openai-subagent": "true",
      },
      body: JSON.stringify({ model: "gpt-subagent-auto", input: [], stream: false }),
    });
    return response.status;
  };
  try {
    const configured = cli("auto");
    assert.equal(configured.status, 0, configured.stderr);
    assert.equal(await request(), 200);
    assert.deepEqual(seen, ["official-usage", "official"]);
    gptUsed = 95;
    assert.equal(await request(), 200);
    assert.deepEqual(seen.slice(2), ["official-usage", "supergpt"]);
    assert.equal(seen.includes("wlb"), false);
    gptUsed = 0;
    assert.equal(await request(), 200);
    assert.deepEqual(seen.slice(4), ["official-usage", "official"]);
    assert.equal(JSON.parse(readFileSync(path.join(router.stateDir, "subagent-route.json"))).route, "auto");
    assert.equal(existsSync(path.join(router.stateDir, "auto-route-status.json")), false);
    assert.equal(existsSync(path.join(router.stateDir, "subagent-auto-route-status.json")), true);
  } finally {
    await router.close();
    await close(official);
    await close(supergpt);
    await close(wlb);
  }
});
