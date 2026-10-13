const MAX_SSE_EVENT_BYTES = 8 * 1024 * 1024;

// Fixed blocks bound both payload bytes and metadata even for one-byte chunks.
export class SseEventBuffer {
  #chunks = [];
  #bytes = 0;
  #lineBytes = 0;
  #cr = false;
  #boundary = false;
  #dropping = false;
  #onEvent;
  #onOverflow;

  constructor(onEvent, onOverflow) {
    this.#onEvent = onEvent;
    this.#onOverflow = onOverflow;
  }

  #append(bytes) {
    if (!bytes.length) return;
    if (this.#dropping) {
      this.#onOverflow(bytes);
      return;
    }
    if (this.#bytes + bytes.length > MAX_SSE_EVENT_BYTES) {
      if (!this.#onOverflow) throw new Error("SSE event exceeds 8 MiB buffer limit");
      for (const chunk of this.#retainedChunks()) this.#onOverflow(chunk);
      this.#chunks = [];
      this.#bytes = 0;
      this.#dropping = true;
      this.#onOverflow(bytes);
      return;
    }
    let offset = 0;
    while (offset < bytes.length) {
      const used = this.#bytes % (64 * 1024);
      if (used === 0) this.#chunks.push(Buffer.allocUnsafe(64 * 1024));
      const copied = bytes.copy(this.#chunks.at(-1), used, offset, offset + 64 * 1024 - used);
      offset += copied;
      this.#bytes += copied;
    }
  }

  #retainedChunks() {
    return this.#chunks.map((chunk, index) =>
      index === this.#chunks.length - 1 && this.#bytes % (64 * 1024)
        ? chunk.subarray(0, this.#bytes % (64 * 1024))
        : chunk,
    );
  }

  #emit() {
    if (!this.#dropping && this.#bytes) {
      this.#onEvent(Buffer.concat(this.#chunks, this.#bytes));
    }
    this.#chunks = [];
    this.#bytes = 0;
    this.#dropping = false;
    this.#boundary = false;
  }

  write(chunk) {
    let start = 0;
    for (let index = 0; index < chunk.length; index += 1) {
      const byte = chunk[index];
      if (this.#cr) {
        this.#cr = false;
        if (byte === 0x0a) {
          const bytes = chunk.subarray(start, index + 1);
          if (this.#boundary) {
            this.#onEvent(bytes);
            this.#boundary = false;
          } else {
            this.#append(bytes);
          }
          start = index + 1;
          continue;
        }
        this.#boundary = false;
      }
      if (byte !== 0x0a && byte !== 0x0d) {
        this.#lineBytes += 1;
        continue;
      }
      this.#append(chunk.subarray(start, index + 1));
      start = index + 1;
      this.#boundary = this.#lineBytes === 0;
      this.#lineBytes = 0;
      if (byte === 0x0d) {
        this.#cr = true;
        if (this.#boundary) {
          this.#emit();
          // A CR already ends the event; a following LF is framing only.
          this.#boundary = true;
        }
      } else if (this.#boundary) this.#emit();
    }
    this.#append(chunk.subarray(start));
  }

  end() {
    this.#emit();
    this.#cr = false;
    this.#lineBytes = 0;
  }
}

function eventLines(bytes) {
  const lines = bytes.toString("utf8").match(/[^\r\n]*(?:\r\n|\r|\n|$)/g) || [];
  return lines.filter(Boolean).map((raw, index) => {
    const ending = raw.match(/(?:\r\n|\r|\n)$/)?.[0] || "";
    const text = ending ? raw.slice(0, -ending.length) : raw;
    const bom = index === 0 && text.startsWith("\uFEFF") ? "\uFEFF" : "";
    return { raw, ending, bom, content: bom ? text.slice(1) : text };
  });
}

export function parseSseEvent(bytes) {
  const data = eventLines(bytes)
    .filter((line) => line.content.startsWith("data:"))
    .map((line) => line.content.slice(5).replace(/^ /, ""))
    .join("\n");
  if (!data || data.trim() === "[DONE]") return undefined;
  try {
    return JSON.parse(data);
  } catch {
    return undefined;
  }
}

export function renderSseEvent(bytes, payload, { rewriteEventType = false } = {}) {
  let dataWritten = false;
  const lines = eventLines(bytes).map((line) => {
    if (rewriteEventType && line.content.startsWith("event:")) {
      return line.bom + "event: " + payload.type + line.ending;
    }
    if (!line.content.startsWith("data:")) return line.raw;
    if (dataWritten) return "";
    dataWritten = true;
    return line.bom + "data: " + JSON.stringify(payload) + line.ending;
  });
  return Buffer.from(lines.join(""), "utf8");
}
