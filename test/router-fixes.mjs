import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { once } from "node:events";
import { mkdtempSync, rmSync } from "node:fs";
import http from "node:http";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { fileURLToPath } from "node:url";

import { ResponseUsageTransform } from "../src/response-usage.mjs";

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
  } finally {
    router.kill("SIGTERM");
    if (router.exitCode === null) await once(router, "exit");
    await close(mimo);
    rmSync(stateDir, { recursive: true, force: true });
  }
});
