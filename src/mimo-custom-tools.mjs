import { Transform } from "node:stream";
import { StringDecoder } from "node:string_decoder";

const CUSTOM_PARAMETERS = {
  type: "object",
  properties: { input: { type: "string" } },
  required: ["input"],
  additionalProperties: false,
};

const SSE_FIELD_LINE = /^(?:event|data):/m;
const SSE_SNIFF_BYTES = 512;
const MAX_JSON_CAPTURE_BYTES = 8 * 1024 * 1024;

function customParameters() {
  return {
    ...CUSTOM_PARAMETERS,
    properties: { input: { type: "string" } },
    required: ["input"],
  };
}

function decodeInput(argumentsValue) {
  if (argumentsValue && typeof argumentsValue === "object" && !Array.isArray(argumentsValue)) {
    return Object.prototype.hasOwnProperty.call(argumentsValue, "input")
      ? argumentsValue.input
      : argumentsValue;
  }
  if (typeof argumentsValue !== "string") return argumentsValue;
  try {
    const parsed = JSON.parse(argumentsValue);
    if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) {
      return Object.prototype.hasOwnProperty.call(parsed, "input")
        ? parsed.input
        : argumentsValue;
    }
  } catch {
    // A provider can send a non-JSON argument string; preserve it as custom input.
  }
  return argumentsValue;
}

function customToolCall(item, argumentsValue = item.arguments) {
  const { arguments: _arguments, type: _type, ...rest } = item;
  return {
    ...rest,
    type: "custom_tool_call",
    input: decodeInput(argumentsValue),
  };
}

function mapResponseItem(item, names) {
  if (item?.type !== "function_call" || !names.has(item.name)) return item;
  return customToolCall(item);
}

function mapOutputArray(output, names) {
  if (!Array.isArray(output)) return output;
  let changed = false;
  const mapped = output.map((item) => {
    const next = mapResponseItem(item, names);
    changed ||= next !== item;
    return next;
  });
  return changed ? mapped : output;
}

export function convertMimoRequestTools(tools) {
  if (!Array.isArray(tools)) return { tools, names: new Set() };
  const names = new Set();
  let changed = false;
  const converted = tools.map((tool) => {
    if (tool?.type !== "custom" || typeof tool.name !== "string" || !tool.name) {
      return tool;
    }
    names.add(tool.name);
    changed = true;
    return {
      type: "function",
      name: tool.name,
      ...(tool.description === undefined ? {} : { description: tool.description }),
      parameters: customParameters(),
    };
  });
  return { tools: changed ? converted : tools, names };
}

export function convertMimoRequestInput(input) {
  if (!Array.isArray(input)) return input;
  return input.map((item) => {
    if (item?.type === "custom_tool_call") {
      const { input: value, type: _type, ...rest } = item;
      return {
        ...rest,
        type: "function_call",
        arguments: JSON.stringify({ input: value }),
      };
    }
    if (item?.type === "custom_tool_call_output") {
      return { ...item, type: "function_call_output" };
    }
    return item;
  });
}

export function convertMimoResponseJson(payload, names) {
  if (!names?.size || !payload || typeof payload !== "object" || Array.isArray(payload)) {
    return payload;
  }
  const direct = mapResponseItem(payload, names);
  if (direct !== payload) return direct;

  let changed = false;
  const next = { ...payload };
  if (Array.isArray(payload.output)) {
    next.output = mapOutputArray(payload.output, names);
    changed ||= next.output !== payload.output;
  }
  if (payload.response && typeof payload.response === "object" && !Array.isArray(payload.response)) {
    const response = { ...payload.response };
    let responseChanged = false;
    if (Array.isArray(payload.response.output)) {
      response.output = mapOutputArray(payload.response.output, names);
      responseChanged ||= response.output !== payload.response.output;
    }
    if (payload.response.item && typeof payload.response.item === "object") {
      response.item = mapResponseItem(payload.response.item, names);
      responseChanged ||= response.item !== payload.response.item;
    }
    if (responseChanged) {
      next.response = response;
      changed = true;
    }
  }
  if (payload.item && typeof payload.item === "object") {
    next.item = mapResponseItem(payload.item, names);
    changed ||= next.item !== payload.item;
  }
  return changed ? next : payload;
}

function parseSseData(block) {
  const data = block
    .split(/\r?\n/)
    .filter((line) => line.startsWith("data:"))
    .map((line) => line.slice(5).trimStart())
    .join("\n")
    .trim();
  if (!data || data === "[DONE]") return undefined;
  try {
    return JSON.parse(data);
  } catch {
    return undefined;
  }
}

function renderSseEvent(block, event) {
  const lines = block.split(/\r?\n/);
  const rendered = [];
  let dataWritten = false;
  for (const line of lines) {
    if (line.startsWith("event:")) {
      rendered.push(`event: ${event.type}`);
      continue;
    }
    if (line.startsWith("data:")) {
      if (!dataWritten) {
        rendered.push(`data: ${JSON.stringify(event)}`);
        dataWritten = true;
      }
      continue;
    }
    rendered.push(line);
  }
  if (!dataWritten) rendered.push(`data: ${JSON.stringify(event)}`);
  return rendered.join("\n");
}

function eventItemKey(item) {
  return [item?.id, item?.call_id].filter((value) => typeof value === "string" && value);
}

export class MimoCustomToolCallTransform extends Transform {
  #names;
  #eventStream;
  #json;
  #undecided;
  #decoder = new StringDecoder("utf8");
  #sseBuffer = "";
  #jsonBuffer = Buffer.alloc(0);
  #passthrough = false;
  #states = new Map();

  constructor(names, contentType = "") {
    super();
    this.#names = names instanceof Set ? names : new Set(names || []);
    const declared = String(contentType).toLowerCase();
    this.#eventStream = declared.includes("text/event-stream");
    this.#json = declared.includes("json");
    this.#undecided = !this.#eventStream && !this.#json;
  }

  _transform(chunk, _encoding, callback) {
    if (this.#undecided && chunk.length) {
      const text = chunk.subarray(0, SSE_SNIFF_BYTES).toString("utf8");
      this.#undecided = false;
      if (SSE_FIELD_LINE.test(text)) this.#eventStream = true;
      else if (/^\s*[\[{]/.test(text)) this.#json = true;
      else this.#passthrough = true;
    }
    if (this.#passthrough || !this.#names.size) {
      this.push(chunk);
      callback();
      return;
    }
    if (this.#eventStream) {
      this.#sseBuffer += this.#decoder.write(chunk);
      this.#emitSseEvents();
      callback();
      return;
    }
    if (this.#json) {
      this.#jsonBuffer = this.#jsonBuffer.length
        ? Buffer.concat([this.#jsonBuffer, chunk])
        : chunk;
      if (this.#jsonBuffer.length > MAX_JSON_CAPTURE_BYTES) {
        this.push(this.#jsonBuffer);
        this.#jsonBuffer = Buffer.alloc(0);
        this.#passthrough = true;
      }
      callback();
      return;
    }
    this.push(chunk);
    callback();
  }

  _flush(callback) {
    if (this.#passthrough || !this.#names.size) {
      callback();
      return;
    }
    if (this.#eventStream) {
      this.#sseBuffer += this.#decoder.end();
      this.#emitSseEvents(true);
      callback();
      return;
    }
    if (this.#json && this.#jsonBuffer.length) {
      const body = this.#jsonBuffer;
      this.#jsonBuffer = Buffer.alloc(0);
      try {
        const payload = JSON.parse(body.toString("utf8"));
        const mapped = convertMimoResponseJson(payload, this.#names);
        this.push(mapped === payload ? body : Buffer.from(JSON.stringify(mapped), "utf8"));
      } catch {
        this.push(body);
      }
    }
    callback();
  }

  #remember(item) {
    const state = {
      item,
      fragments: "",
      firstDelta: undefined,
      input: undefined,
      inputReady: false,
    };
    for (const key of eventItemKey(item)) this.#states.set(key, state);
    return state;
  }

  #stateFor(event) {
    return [event?.item_id, event?.call_id]
      .map((key) => this.#states.get(key))
      .find(Boolean);
  }

  #forget(state) {
    for (const [key, candidate] of this.#states) {
      if (candidate === state) this.#states.delete(key);
    }
  }

  #mapItem(item, state) {
    if (item?.type !== "function_call" || !this.#names.has(item.name)) return undefined;
    const raw =
      state?.inputReady
        ? state.input
        : item.arguments !== undefined && item.arguments !== ""
          ? item.arguments
          : state?.fragments || item.arguments;
    const mapped = customToolCall(item, raw);
    return mapped;
  }

  #rewriteEvent(event) {
    if (event?.type === "response.output_item.added" && event.item) {
      if (event.item.type !== "function_call" || !this.#names.has(event.item.name)) return undefined;
      const state = this.#remember(event.item);
      const item = this.#mapItem(event.item, state);
      return [{ ...event, item }];
    }
    if (event?.type === "response.output_item.done" && event.item) {
      if (event.item.type !== "function_call" || !this.#names.has(event.item.name)) return undefined;
      const state = this.#stateFor({ item_id: event.item.id, call_id: event.item.call_id });
      const item = this.#mapItem(event.item, state);
      if (state) this.#forget(state);
      return [{ ...event, item }];
    }
    if (event?.type === "response.function_call_arguments.delta") {
      const state = this.#stateFor(event);
      if (!state || typeof event.delta !== "string") return undefined;
      state.fragments += event.delta;
      state.firstDelta ||= event;
      return [];
    }
    if (event?.type === "response.function_call_arguments.done") {
      const state = this.#stateFor(event);
      if (!state) return undefined;
      const raw = event.arguments !== undefined && event.arguments !== ""
        ? event.arguments
        : state.fragments;
      const input = decodeInput(raw);
      state.input = input;
      state.inputReady = true;
      const output = [];
      if (state.firstDelta) {
        const { arguments: _arguments, ...base } = state.firstDelta;
        output.push({
          ...base,
          type: "response.custom_tool_call_input.delta",
          delta: input,
        });
      }
      const { arguments: _arguments, ...base } = event;
      output.push({ ...base, type: "response.custom_tool_call_input.done", input });
      return output;
    }
    if (event?.type === "response.completed" && event.response) {
      const response = { ...event.response };
      const output = mapOutputArray(response.output, this.#names);
      if (output === response.output) return undefined;
      response.output = output;
      return [{ ...event, response }];
    }
    return undefined;
  }

  #emitSseEvents(flush = false) {
    while (true) {
      const match = /\r?\n\r?\n/.exec(this.#sseBuffer);
      if (!match) break;
      const block = this.#sseBuffer.slice(0, match.index);
      this.#sseBuffer = this.#sseBuffer.slice(match.index + match[0].length);
      this.#emitSseBlock(block, match[0]);
    }
    if (flush && this.#sseBuffer) {
      const block = this.#sseBuffer;
      this.#sseBuffer = "";
      this.#emitSseBlock(block, "");
    }
  }

  #emitSseBlock(block, separator) {
    const event = parseSseData(block);
    const rewritten = event ? this.#rewriteEvent(event) : undefined;
    if (rewritten === undefined) {
      this.push(Buffer.from(block + separator, "utf8"));
      return;
    }
    for (const next of rewritten) {
      this.push(Buffer.from(`${renderSseEvent(block, next)}${separator}`, "utf8"));
    }
  }
}
