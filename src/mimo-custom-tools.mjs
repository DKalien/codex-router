import { Transform } from "node:stream";

import { HeaderlessSseDetector } from "./sse-prefix.mjs";

import { SseEventBuffer, parseSseEvent, renderSseEvent } from "./sse-events.mjs";

const CUSTOM_PARAMETERS = {
  type: "object",
  properties: { input: { type: "string" } },
  required: ["input"],
  additionalProperties: false,
};

const MAX_JSON_CAPTURE_BYTES = 8 * 1024 * 1024;
const NATIVE_ENCRYPTED_CONTENT = /^gAAAAA[A-Za-z0-9_-]+={0,2}$/;

function isOpaqueNativeEncryptedContent(value) {
  return typeof value === "string" && NATIVE_ENCRYPTED_CONTENT.test(value);
}

function collaborationMessageType(item) {
  if (!Array.isArray(item?.content)) return undefined;
  const visible = item.content
    .filter(
      (part) =>
        ["input_text", "text"].includes(part?.type) && typeof part.text === "string",
    )
    .map((part) => part.text)
    .join("");
  return visible.match(/(?:^|\n)\s*Message Type:\s*(NEW_TASK|MESSAGE|FOLLOWUP_TASK|FINAL_ANSWER)\b/i)?.[1]?.toUpperCase();
}

export function pruneMimoAgentProgress(input) {
  if (!Array.isArray(input)) return input;
  // ponytail: MiMo replays accepted handoffs, not transient progress; restore
  // MESSAGE when Xiaomi accepts agent_message or native batch relay is reliable.
  return input.filter((item) =>
    item?.type !== "agent_message" || collaborationMessageType(item) !== "MESSAGE",
  );
}

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
  return input.flatMap((item) => {
    if (item?.type === "agent_message") {
      if (!Array.isArray(item.content)) return [];
      const content = [];
      for (const part of item.content) {
        if (
          part?.type === "input_text" &&
          typeof part.text === "string" &&
          part.text.trim() !== ""
        ) {
          content.push({ type: "input_text", text: part.text });
        } else if (
          part?.type === "encrypted_content" &&
          typeof part.encrypted_content === "string" &&
          part.encrypted_content.trim() !== "" &&
          !isOpaqueNativeEncryptedContent(part.encrypted_content)
        ) {
          content.push({ type: "input_text", text: part.encrypted_content });
        }
      }
      return content.length ? [{ type: "message", role: "user", content }] : [];
    }
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
    return [item];
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

function eventItemKey(item) {
  return [item?.id, item?.call_id].filter((value) => typeof value === "string" && value);
}

export class MimoCustomToolCallTransform extends Transform {
  #names;
  #eventStream;
  #json;
  #headerlessDetector;
  #events;
  #jsonBuffer = [];
  #jsonBytes = 0;
  #passthrough = false;
  #states = new Map();
  #stateBytes = 0;

  constructor(names, contentType = "") {
    super();
    this.#names = names instanceof Set ? names : new Set(names || []);
    this.#events = new SseEventBuffer((bytes) => this.#emitSseBlock(bytes));
    const declared = String(contentType).toLowerCase();
    this.#eventStream = declared.includes("text/event-stream");
    this.#json = declared.includes("json");
    this.#headerlessDetector =
      !this.#eventStream && !this.#json && this.#names.size
        ? new HeaderlessSseDetector()
        : undefined;
  }

  _transform(chunk, _encoding, callback) {
    try {
      if (this.#headerlessDetector) {
        const detected = this.#headerlessDetector.write(chunk);
        if (detected.decision === "pending") {
          callback();
          return;
        }
        this.#headerlessDetector = undefined;
        this.#eventStream = detected.decision === "event-stream";
        if (!this.#eventStream) {
          const prefix = detected.chunks[0] || Buffer.alloc(0);
          this.#json = /^\s*[\[{]/.test(prefix.subarray(0, 512).toString("utf8"));
          this.#passthrough = !this.#json;
        }
        for (const buffered of detected.chunks) this.#transformChunk(buffered);
        callback();
        return;
      }
      this.#transformChunk(chunk);
      callback();
    } catch (error) {
      callback(error);
    }
  }

  #transformChunk(chunk) {
    if (this.#passthrough || !this.#names.size) {
      this.push(chunk);
      return;
    }
    if (this.#eventStream) {
      this.#events.write(chunk);
      return;
    }
    if (this.#json) {
      if (this.#jsonBytes + chunk.length > MAX_JSON_CAPTURE_BYTES) {
        throw new Error("MiMo JSON response exceeds 8 MiB buffer limit");
      }
      let offset = 0;
      while (offset < chunk.length) {
        const used = this.#jsonBytes % (64 * 1024);
        if (used === 0) this.#jsonBuffer.push(Buffer.allocUnsafe(64 * 1024));
        const copied = chunk.copy(this.#jsonBuffer.at(-1), used, offset, offset + 64 * 1024 - used);
        offset += copied;
        this.#jsonBytes += copied;
      }
      return;
    }
    this.push(chunk);
  }

  _flush(callback) {
    try {
      if (this.#headerlessDetector) {
        const detected = this.#headerlessDetector.end();
        this.#headerlessDetector = undefined;
        this.#eventStream = detected.decision === "event-stream";
        if (!this.#eventStream) {
          const prefix = detected.chunks[0] || Buffer.alloc(0);
          this.#json = /^\s*[\[{]/.test(prefix.subarray(0, 512).toString("utf8"));
          this.#passthrough = !this.#json;
        }
        for (const buffered of detected.chunks) this.#transformChunk(buffered);
      }
      if (this.#passthrough || !this.#names.size) {
        callback();
        return;
      }
      if (this.#eventStream) {
        this.#events.end();
        callback();
        return;
      }
      if (this.#json && this.#jsonBytes) {
        const body = Buffer.concat(this.#jsonBuffer, this.#jsonBytes);
        this.#jsonBuffer = [];
        this.#jsonBytes = 0;
        try {
          const payload = JSON.parse(body.toString("utf8"));
          const mapped = convertMimoResponseJson(payload, this.#names);
          this.push(mapped === payload ? body : Buffer.from(JSON.stringify(mapped), "utf8"));
        } catch {
          this.push(body);
        }
      }
      callback();
    } catch (error) {
      callback(error);
    }
  }

  #reserveState(bytes) {
    if (this.#stateBytes + bytes > MAX_JSON_CAPTURE_BYTES) {
      throw new Error("MiMo tool arguments exceed 8 MiB buffer limit");
    }
    this.#stateBytes += bytes;
  }

  #remember(item) {
    const keys = eventItemKey(item);
    for (const key of keys) {
      const previous = this.#states.get(key);
      if (previous) this.#forget(previous);
    }
    const bytes = keys.length ? Buffer.byteLength(JSON.stringify(item)) : 0;
    this.#reserveState(bytes);
    const state = {
      bytes,
      fragmentBytes: 0,
      item,
      fragments: "",
      firstDelta: undefined,
      input: undefined,
      inputReady: false,
    };
    for (const key of keys) this.#states.set(key, state);
    return state;
  }

  #stateFor(event) {
    return [event?.item_id, event?.call_id]
      .map((key) => this.#states.get(key))
      .find(Boolean);
  }

  #forget(state) {
    this.#stateBytes -= state.bytes;
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
      const bytes = Buffer.byteLength(event.delta) +
        (state.firstDelta ? 0 : Buffer.byteLength(JSON.stringify(event)));
      this.#reserveState(bytes);
      state.bytes += bytes;
      state.fragmentBytes += bytes;
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
      const bytes = Buffer.byteLength(JSON.stringify(input) ?? "");
      this.#stateBytes -= state.fragmentBytes;
      state.bytes -= state.fragmentBytes;
      this.#reserveState(bytes);
      state.bytes += bytes;
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
      state.fragments = "";
      state.firstDelta = undefined;
      state.fragmentBytes = 0;
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

  #emitSseBlock(bytes) {
    const event = parseSseEvent(bytes);
    const rewritten = event ? this.#rewriteEvent(event) : undefined;
    if (rewritten === undefined) {
      this.push(bytes);
      return;
    }
    for (const next of rewritten) {
      this.push(renderSseEvent(bytes, next, { rewriteEventType: true }));
    }
  }
}
