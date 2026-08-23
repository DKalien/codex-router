import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { once } from "node:events";
import { existsSync, mkdtempSync, readFileSync, rmSync } from "node:fs";
import http from "node:http";
import net from "node:net";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

import { ResponseUsageTransform } from "../src/response-usage.mjs";
import { readResponseTextLimited } from "../src/http-utils.mjs";
import { sanitizeUpstreamText } from "../src/error-translation.mjs";
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
    assert.equal(response.status, 200);
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
