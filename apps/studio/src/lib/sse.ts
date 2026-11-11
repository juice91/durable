// Incremental SSE parser for fetch-based streams (EventSource cannot send an Authorization header).
// Accepts a whole string, a ReadableStream<string>, or a fetch body (ReadableStream<Uint8Array>):
// bytes are decoded with a streaming TextDecoder, so a multi-byte UTF-8 character split across
// chunks is decoded correctly. Events are yielded as soon as their terminating blank line arrives.
// `:` comment lines (heartbeats) are ignored; an incomplete event at end of stream is discarded
// (per the SSE spec), so a dropped connection never yields a truncated frame.

export interface SSEEvent {
  event: string;
  id?: string;
  data: unknown;
}

export type SSESource = string | ReadableStream<string> | ReadableStream<Uint8Array>;

interface Pending {
  event?: string;
  id?: string;
  data: string[];
}

function parseData(lines: string[]): unknown {
  const raw = lines.join("\n");
  try {
    return JSON.parse(raw);
  } catch {
    return raw;
  }
}

export async function* parseSSE(source: SSESource): AsyncGenerator<SSEEvent, void, unknown> {
  const reader =
    typeof source === "string"
      ? null
      : (source as ReadableStream<string | Uint8Array>).getReader();
  const decoder = new TextDecoder("utf-8");

  let buffer = "";
  let pending: Pending = { data: [] };

  // Process every complete line in `buffer`, yielding dispatched events.
  function* drain(): Generator<SSEEvent> {
    let nl: number;
    while ((nl = buffer.indexOf("\n")) !== -1) {
      let line = buffer.slice(0, nl);
      buffer = buffer.slice(nl + 1);
      if (line.endsWith("\r")) line = line.slice(0, -1);

      if (line === "") {
        if (pending.data.length > 0 || pending.event !== undefined) {
          const out: SSEEvent = { event: pending.event ?? "message", data: parseData(pending.data) };
          if (pending.id !== undefined) out.id = pending.id;
          yield out;
        }
        pending = { data: [] };
        continue;
      }
      if (line.startsWith(":")) continue;

      const colon = line.indexOf(":");
      const field = colon === -1 ? line : line.slice(0, colon);
      let value = colon === -1 ? "" : line.slice(colon + 1);
      if (value.startsWith(" ")) value = value.slice(1);

      switch (field) {
        case "event":
          pending.event = value;
          break;
        case "id":
          pending.id = value;
          break;
        case "data":
          pending.data.push(value);
          break;
      }
    }
  }

  if (reader === null) {
    buffer = source as string;
    if (!buffer.endsWith("\n")) buffer += "\n";
    yield* drain();
    return;
  }

  try {
    while (true) {
      const { value, done } = await reader.read();
      if (done) {
        buffer += decoder.decode();
        // A trailing line without its newline is still a complete field line, but an event
        // without its terminating blank line is incomplete and dropped.
        if (buffer.length > 0) buffer += "\n";
        yield* drain();
        return;
      }
      buffer += typeof value === "string" ? value : decoder.decode(value, { stream: true });
      yield* drain();
    }
  } finally {
    try {
      await reader.cancel();
      reader.releaseLock();
    } catch {
      // Already closed or errored.
    }
  }
}
