import assert from "node:assert/strict";
import { once } from "node:events";
import test from "node:test";

import { MimoCustomToolCallTransform } from "../src/mimo-custom-tools.mjs";
import {
  estimateInputTokens,
  ResponseUsageTransform,
  substituteZeroInputUsage,
} from "../src/response-usage.mjs";

async function collect(transform, chunks) {
  const output = [];
  transform.on("data", (chunk) => output.push(chunk));
  const finished = once(transform, "finish");
  for (const chunk of chunks) transform.write(chunk);
  transform.end();
  await finished;
  return Buffer.concat(output);
}

test("headerless usage survives split event field and preserves bytes", async () => {
  const body = Buffer.from(
    'event: response.completed\ndata: {"type":"response.completed","response":{"usage":{"input_tokens":4,"output_tokens":2}}}\n\n',
    "utf8",
  );
  const transform = new ResponseUsageTransform("");
  const output = await collect(transform, [body.subarray(0, 2), body.subarray(2)]);

  assert.deepEqual(output, body);
  assert.deepEqual(transform.tokenUsage(), {
    inputTokens: 4,
    outputTokens: 2,
    totalTokens: 6,
  });
});

test("headerless usage accepts BOM, comments, blank lines, and tiny chunks", async () => {
  const body = Buffer.from(
    '\uFEFF: keepalive\r\n\r\n\n' +
      'event: response.completed\ndata: {"type":"response.completed","response":{"usage":{"input_tokens":7,"output_tokens":3}}}\n\n',
    "utf8",
  );
  const transform = new ResponseUsageTransform("");
  const output = await collect(transform, [...body].map((byte) => Buffer.from([byte])));

  assert.deepEqual(output, body);
  assert.deepEqual(transform.tokenUsage(), {
    inputTokens: 7,
    outputTokens: 3,
    totalTokens: 10,
  });
});

test("BOM attached to the first data line remains parseable and byte-exact", async () => {
  const body = Buffer.from(
    '\uFEFFdata: {"type":"response.completed","response":{"usage":{"input_tokens":9,"output_tokens":4}}}\n\n',
    "utf8",
  );
  const transform = new ResponseUsageTransform("");

  assert.deepEqual(await collect(transform, [body.subarray(0, 1), body.subarray(1)]), body);
  assert.deepEqual(transform.tokenUsage(), {
    inputTokens: 9,
    outputTokens: 4,
    totalTokens: 13,
  });
});

test("MiMo rewrites headerless SSE when event is split across chunks", async () => {
  const event = {
    type: "response.output_item.added",
    item: {
      id: "item_1",
      type: "function_call",
      name: "lookup",
      call_id: "call_1",
      arguments: '{"input":"x"}',
    },
  };
  const body = Buffer.from(
    `event: response.output_item.added\ndata: ${JSON.stringify(event)}\n\n`,
    "utf8",
  );
  const transform = new MimoCustomToolCallTransform(new Set(["lookup"]), "");
  const output = await collect(transform, [body.subarray(0, 2), body.subarray(2)]);
  const text = output.toString("utf8");

  assert.match(text, /"type":"custom_tool_call"/);
  assert.doesNotMatch(text, /"type":"function_call","name":"lookup"/);
});

test("MiMo rewrites a first data line with BOM and preserves the BOM", async () => {
  const event = {
    type: "response.output_item.added",
    item: {
      id: "item_bom",
      type: "function_call",
      name: "lookup",
      call_id: "call_bom",
      arguments: '{"input":"x"}',
    },
  };
  const body = Buffer.from(`\uFEFFdata: ${JSON.stringify(event)}\n\n`, "utf8");
  const transform = new MimoCustomToolCallTransform(new Set(["lookup"]), "");
  const output = await collect(transform, [body.subarray(0, 2), body.subarray(2)]);

  assert.equal(output.subarray(0, 3).equals(Buffer.from([0xef, 0xbb, 0xbf])), true);
  assert.match(output.toString("utf8"), /"type":"custom_tool_call"/);
});

test("encrypted ciphertext does not inflate the input estimate", () => {
  const visible = { type: "message", role: "user", content: [{ type: "input_text", text: "x".repeat(40_000) }] };
  const withCiphertext = Buffer.from(
    JSON.stringify({
      model: "m",
      input: [visible, { type: "reasoning", encrypted_content: `gAAAAAB${"x".repeat(300_000)}` }],
    }),
    "utf8",
  );
  const withoutCiphertext = Buffer.from(
    JSON.stringify({ model: "m", input: [visible, { type: "reasoning" }] }),
    "utf8",
  );

  assert.ok(withCiphertext.byteLength > withoutCiphertext.byteLength * 8);
  assert.ok(
    Math.abs(estimateInputTokens(withCiphertext) - estimateInputTokens(withoutCiphertext)) <= 40,
  );
});

test("unknown and non-JSON bytes remain fully counted", () => {
  const opaque = Buffer.alloc(64_000, 0x9c);
  assert.equal(estimateInputTokens(opaque), Math.ceil(opaque.byteLength / 3.3));

  const unknown = Buffer.from(JSON.stringify({ input: [{ unknown: "z".repeat(20_000) }] }), "utf8");
  assert.equal(estimateInputTokens(unknown), Math.ceil(unknown.byteLength / 3.3));
});

test("headerless JSON and non-JSON bodies are not treated as SSE", async () => {
  const json = Buffer.from('{"usage":{"input_tokens":5,"output_tokens":1}}', "utf8");
  const jsonTransform = new ResponseUsageTransform("");
  assert.deepEqual(await collect(jsonTransform, [json.subarray(0, 1), json.subarray(1)]), json);
  assert.deepEqual(jsonTransform.tokenUsage(), {
    inputTokens: 5,
    outputTokens: 1,
    totalTokens: 6,
  });

  const opaque = Buffer.from("not an event stream", "utf8");
  const opaqueTransform = new ResponseUsageTransform("");
  assert.deepEqual(await collect(opaqueTransform, [opaque.subarray(0, 3), opaque.subarray(3)]), opaque);
  assert.equal(opaqueTransform.tokenUsage(), undefined);
});

test("only explicit zero prompt usage is replaced", () => {
  assert.deepEqual(
    substituteZeroInputUsage({ usage: { input_tokens: 0, output_tokens: 2 } }, 1_200)?.usage,
    { input_tokens: 1_200, output_tokens: 2, total_tokens: 1_202 },
  );
  assert.equal(substituteZeroInputUsage({ usage: { input_tokens: null, output_tokens: 2 } }, 1_200), undefined);
  assert.equal(substituteZeroInputUsage({ usage: { input_tokens: 3, output_tokens: 2 } }, 1_200), undefined);
});
test("usage parses multiline data with CR, LF and CRLF across byte chunks", async () => {
  for (const ending of ["\r", "\n", "\r\n"]) {
    for (const estimate of [undefined, 1200]) {
      const body = Buffer.from('\uFEFF\rdata: {"type":"response.completed",\rdata: "response":{"usage":{"input_tokens":0,"output_tokens":2}}}\r\r');
      const framed = Buffer.from(body.toString().replace(/\r/g, ending));
      const transform = new ResponseUsageTransform("", { estimatedInputTokens: estimate });
      const output = await collect(transform, [...framed].map((byte) => Buffer.from([byte])));
      assert.equal(transform.completedResponseObserved(), true);
      assert.equal(transform.tokenUsage()?.outputTokens, 2);
      if (estimate) assert.match(output.toString(), /"input_tokens":1200/);
      else assert.deepEqual(output, framed);
    }
  }
});
test("oversized SSE releases bytes before newline and resumes usage", async () => {
  const transform = new ResponseUsageTransform("text/event-stream", { estimatedInputTokens: 1200 });
  let forwarded = 0;
  transform.on("data", (chunk) => { forwarded += chunk.length; });
  const chunk = Buffer.alloc(64 * 1024, 0x78);
  transform.write(Buffer.from("data: "));
  for (let index = 0; index < 129; index += 1) transform.write(chunk);
  assert.ok(forwarded > 8 * 1024 * 1024);
  const finished = once(transform, "finish");
  transform.end('\n\ndata: {"type":"response.completed","response":{"usage":{"input_tokens":0,"output_tokens":2}}}\n\n');
  await finished;
  assert.equal(transform.completedResponseObserved(), true);
  assert.equal(transform.substitutedInputTokens(), 1200);
});
test("MiMo restores CR events and preserves unrelated non-UTF8 bytes", async () => {
  const body = Buffer.from('\uFEFFdata: {"type":"response.output_item.added",\rdata: "item":{"type":"function_call","name":"lookup","arguments":"x"}}\r\r');
  const transform = new MimoCustomToolCallTransform(new Set(["lookup"]), "");
  const output = await collect(transform, [...body].map((byte) => Buffer.from([byte])));
  assert.match(output.toString(), /"type":"custom_tool_call"/);
  const untouched = Buffer.from([0x3a, 0xff, 0x0d, 0x0a, 0x0d, 0x0a]);
  assert.deepEqual(await collect(new MimoCustomToolCallTransform(new Set(["lookup"]), "text/event-stream"), [untouched]), untouched);
});
test("MiMo fails explicitly on oversized SSE", async () => {
  await assert.rejects(collect(new MimoCustomToolCallTransform(new Set(["lookup"]), "text/event-stream"), [Buffer.alloc(8 * 1024 * 1024 + 1, 0x78)]), /SSE.*limit/i);
});

test("MiMo bounds JSON and accumulated tool argument deltas", async () => {
  await assert.rejects(collect(new MimoCustomToolCallTransform(new Set(["lookup"]), "application/json"), [Buffer.alloc(8 * 1024 * 1024 + 1, 0x78)]), /JSON.*limit/i);
  const event = (payload) => Buffer.from("data: " + JSON.stringify(payload) + "\n\n");
  const added = event({ type: "response.output_item.added", item: { type: "function_call", id: "large", name: "lookup", arguments: "" } });
  const delta = event({ type: "response.function_call_arguments.delta", item_id: "large", delta: "x".repeat(1024 * 1024) });
  await assert.rejects(collect(new MimoCustomToolCallTransform(new Set(["lookup"]), "text/event-stream"), [added, ...Array(9).fill(delta)]), /arguments.*limit/i);
});

test("usage preserves oversized bytes exactly without repeated whole-event copies", async () => {
  const concat = Buffer.concat;
  let copied = 0;
  Buffer.concat = (chunks, length) => {
    copied += length ?? chunks.reduce((sum, chunk) => sum + chunk.length, 0);
    return concat(chunks, length);
  };
  const chunk = Buffer.alloc(64 * 1024, 0x78);
  const tail = Buffer.from("\r\rdata: {\"usage\":{\"input_tokens\":1}}\r\r");
  const body = [Buffer.from("data: "), ...Array(256).fill(chunk), tail];
  let output;
  try {
    output = await collect(new ResponseUsageTransform("text/event-stream", { estimatedInputTokens: 1200 }), body);
  } finally {
    Buffer.concat = concat;
  }
  assert.deepEqual(output, concat(body));
  assert.ok(copied < 32 * 1024 * 1024, "copies should grow linearly with the input");
});

test("MiMo maps split CRLF argument deltas and keeps mixed framing", async () => {
  const event = (type, fields, ending) =>
    Buffer.from("event: " + type + ending + "data: " + JSON.stringify({ type, ...fields }) + ending + ending);
  const body = Buffer.concat([
    event("response.output_item.added", { item: { id: "split", type: "function_call", name: "lookup", arguments: "" } }, "\r\n"),
    event("response.function_call_arguments.delta", { item_id: "split", delta: '{\"input\":\"ok\"}' }, "\r"),
    event("response.function_call_arguments.done", { item_id: "split" }, "\n"),
    event("response.output_item.done", { item: { id: "split", type: "function_call", name: "lookup", arguments: "" } }, "\r\n"),
  ]);
  const output = await collect(new MimoCustomToolCallTransform(new Set(["lookup"]), "text/event-stream"), [...body].map((byte) => Buffer.from([byte])));
  assert.match(output.toString(), /response.custom_tool_call_input.delta/);
  assert.match(output.toString(), /"input":"ok"/);
  assert.doesNotMatch(output.toString(), /response.function_call_arguments/);
});
test("CR completed events are observed immediately while the stream remains open", () => {
  const transform = new ResponseUsageTransform("text/event-stream");
  transform.resume();
  transform.write('data: {"type":"response.completed","response":{"usage":{"input_tokens":2}}}\r\r');
  assert.equal(transform.completedResponseObserved(), true);
  assert.equal(transform.tokenUsage()?.inputTokens, 2);
  transform.destroy();
});

test("MiMo emits CR tool events immediately and preserves a later split LF", async () => {
  const transform = new MimoCustomToolCallTransform(new Set(["lookup"]), "text/event-stream");
  const output = [];
  transform.on("data", (chunk) => output.push(chunk));
  transform.write('data: {"type":"response.output_item.added","item":{"type":"function_call","name":"lookup","arguments":"x"}}\r\r');
  assert.match(Buffer.concat(output).toString(), /"type":"custom_tool_call"/);
  const beforeLf = Buffer.concat(output);
  transform.write("\n");
  assert.deepEqual(Buffer.concat(output), Buffer.concat([beforeLf, Buffer.from("\n")]));
  const unchanged = Buffer.from('data: {"type":"unrelated"}\r\ndata: {"ignored":true}\r\n\r\n');
  const finished = once(transform, "finish");
  transform.end(unchanged);
  await finished;
  assert.deepEqual(Buffer.concat(output).subarray(beforeLf.length + 1), unchanged);
});
