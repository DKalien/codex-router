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
