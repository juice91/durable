/**
 * Wire capture and conformance for the AG-UI smoke test (AGUI.14).
 *
 * `Wire.fetch` is handed to `HttpAgent` / `ResumableHttpAgent` as their `fetch`. It records every
 * request (method, URL, headers, body) and parses every SSE response as the server sent it, before
 * any client transformation, so the checks see the raw `id:` and `data:` fields. A `cut` rule can
 * drop the connection right after a chosen frame (a network error mid-body), for resume tests.
 *
 * `conformanceErrors` validates each received event with the `@ag-ui/core` zod schemas
 * (`EventSchema`), each POST body with `RunAgentInputSchema`, and each stream's lifecycle (one run,
 * balanced messages, tool calls and steps, frame ids `{runId}:{seq}` never going back, interrupts
 * naming seen tool calls). The lifecycle rules mirror `eap_api.ag_ui.conformance.lifecycle_errors`.
 */
import { EventSchema, RunAgentInputSchema } from "@ag-ui/core/schemas";

export interface Frame {
  id?: string;
  data?: string;
  /** The parsed `data:` JSON (absent for comments and keep-alives). */
  event?: Record<string, unknown>;
}

export interface Exchange {
  method: string;
  url: string;
  /** Request headers, lower-cased names. */
  headers: Record<string, string>;
  /** The parsed JSON request body, if any. */
  body?: unknown;
  status: number;
  frames: Frame[];
  /** True when `cut` dropped this response. */
  cut: boolean;
  /** Resolves when the response body has been read to its end (or cut, or failed). */
  done: Promise<void>;
}

export type CutRule = (frame: Frame, exchange: Exchange) => boolean;

function headerRecord(headers: HeadersInit | undefined): Record<string, string> {
  const out: Record<string, string> = {};
  new Headers(headers ?? {}).forEach((value, key) => {
    out[key.toLowerCase()] = value;
  });
  return out;
}

/** One SSE frame's text (no trailing blank line) as a Frame. */
function parseFrame(text: string): Frame {
  const frame: Frame = {};
  const data: string[] = [];
  for (const line of text.split(/\r?\n/)) {
    if (line === "" || line.startsWith(":")) continue;
    const colon = line.indexOf(":");
    const field = colon < 0 ? line : line.slice(0, colon);
    let value = colon < 0 ? "" : line.slice(colon + 1);
    if (value.startsWith(" ")) value = value.slice(1);
    if (field === "id") frame.id = value;
    else if (field === "data") data.push(value);
  }
  if (data.length > 0) {
    frame.data = data.join("\n");
    frame.event = JSON.parse(frame.data) as Record<string, unknown>;
  }
  return frame;
}

export class Wire {
  readonly exchanges: Exchange[] = [];
  /** Drops the connection right after the first frame this returns true for (once). */
  cut?: CutRule;

  readonly fetch = async (url: string, init: RequestInit = {}): Promise<Response> => {
    const method = (init.method ?? "GET").toUpperCase();
    let body: unknown;
    if (typeof init.body === "string") {
      try {
        body = JSON.parse(init.body);
      } catch {
        body = init.body;
      }
    }
    let finish!: () => void;
    const exchange: Exchange = {
      method,
      url,
      headers: headerRecord(init.headers),
      body,
      status: 0,
      frames: [],
      cut: false,
      done: new Promise<void>((resolve) => (finish = resolve)),
    };
    this.exchanges.push(exchange);
    let response: Response;
    try {
      response = await globalThis.fetch(url, init);
    } catch (error) {
      finish();
      throw error;
    }
    exchange.status = response.status;
    const type = response.headers.get("content-type") ?? "";
    if (!response.body || !type.includes("text/event-stream")) {
      finish();
      return response;
    }
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    const encoder = new TextEncoder();
    let buffer = "";
    const rule = this.cut;
    const self = this;
    const stream = new ReadableStream<Uint8Array>({
      async pull(controller) {
        try {
          const { value, done } = await reader.read();
          if (done) {
            buffer += decoder.decode();
            if (buffer.trim()) {
              exchange.frames.push(parseFrame(buffer));
              controller.enqueue(encoder.encode(buffer));
            }
            controller.close();
            finish();
            return;
          }
          buffer += decoder.decode(value, { stream: true });
          let end: number;
          while ((end = buffer.search(/\r?\n\r?\n/)) >= 0) {
            const sep = buffer.slice(end).match(/^\r?\n\r?\n/)![0];
            const text = buffer.slice(0, end);
            buffer = buffer.slice(end + sep.length);
            const frame = parseFrame(text);
            exchange.frames.push(frame);
            controller.enqueue(encoder.encode(text + sep));
            if (rule && self.cut === rule && rule(frame, exchange)) {
              self.cut = undefined;
              exchange.cut = true;
              await reader.cancel().catch(() => undefined);
              controller.error(new TypeError("smoke: network cut"));
              finish();
              return;
            }
          }
        } catch (error) {
          controller.error(error);
          finish();
        }
      },
      async cancel(reason) {
        await reader.cancel(reason).catch(() => undefined);
        finish();
      },
    });
    return new Response(stream, { status: response.status, statusText: response.statusText, headers: response.headers });
  };

  /** Every exchange's body has been read. */
  async settled(): Promise<void> {
    await Promise.all(this.exchanges.map((e) => e.done));
  }

  /** The streamed exchanges (2xx SSE responses), in order. */
  streams(): Exchange[] {
    return this.exchanges.filter((e) => e.status === 200 && e.frames.length > 0);
  }
}

export function eventsOf(exchange: Exchange): Record<string, unknown>[] {
  return exchange.frames.flatMap((f) => (f.event ? [f.event] : []));
}

/** `TYPE` or `CUSTOM:name` per event. */
export function typesOf(exchange: Exchange): string[] {
  return eventsOf(exchange).map((e) => (e.type === "CUSTOM" ? `CUSTOM:${String(e.name)}` : String(e.type)));
}

interface RunState {
  runId: string | undefined;
  texts: Set<string>;
  tools: Set<string>;
  steps: Set<string>;
  seenTexts: Set<string>;
  seenTools: Set<string>;
  lastSeq: number | undefined;
}

function newState(runId: string | undefined, knownTools: Iterable<string> = [], openSteps: Iterable<string> = []): RunState {
  return {
    runId,
    texts: new Set(),
    tools: new Set(),
    steps: new Set(openSteps),
    seenTexts: new Set(),
    seenTools: new Set(knownTools),
    lastSeq: undefined,
  };
}

export interface LifecycleOptions {
  /** The stream resumes a run already started (no RUN_STARTED expected first). */
  continuation?: boolean;
  runId?: string;
  knownToolCalls?: Iterable<string>;
  knownOpenSteps?: Iterable<string>;
}

/** Lifecycle problems of one stream (empty = conformant). */
export function lifecycleErrors(frames: Frame[], options: LifecycleOptions = {}): string[] {
  const errors: string[] = [];
  let state: RunState | undefined = options.continuation
    ? newState(options.runId, options.knownToolCalls, options.knownOpenSteps)
    : undefined;
  let ended = false;
  frames.forEach((frame, index) => {
    const event = frame.event;
    if (!event) return;
    const kind = String(event.type);
    const where = `#${index} ${kind}`;
    for (const [key, value] of Object.entries(event)) {
      if (value === null && !(kind === "CUSTOM" && key === "value")) errors.push(`${where}: ${key} is null`);
    }
    if (kind === "RUN_STARTED") {
      if (state && !ended) errors.push(`${where}: nested run`);
      if (event.protocolVersion !== "1.0") errors.push(`${where}: protocolVersion must be 1.0`);
      state = newState(event.runId as string);
      ended = false;
      return;
    }
    if (!state || ended) {
      if (kind === "RUN_ERROR" && !state) {
        ended = true;
        return;
      }
      errors.push(`${where}: outside a run`);
      return;
    }
    if (frame.id !== undefined) {
      const cut = frame.id.lastIndexOf(":");
      const head = frame.id.slice(0, cut);
      const tail = frame.id.slice(cut + 1);
      if (cut < 0 || head !== state.runId || !/^-?\d+$/.test(tail)) {
        errors.push(`${where}: frame id ${frame.id} is not ${state.runId}:<seq>`);
      } else {
        const seq = Number(tail);
        if (state.lastSeq !== undefined && seq < state.lastSeq) errors.push(`${where}: frame id seq went back`);
        state.lastSeq = seq;
      }
    }
    switch (kind) {
      case "MESSAGES_SNAPSHOT": {
        for (const m of (event.messages as Record<string, unknown>[] | undefined) ?? []) {
          state.seenTexts.add(String(m.id));
          for (const c of (m.toolCalls as Record<string, unknown>[] | undefined) ?? []) state.seenTools.add(String(c.id));
        }
        break;
      }
      case "TEXT_MESSAGE_START": {
        const mid = String(event.messageId);
        if (state.texts.has(mid)) errors.push(`${where}: ${mid} already open`);
        else if (state.seenTexts.has(mid)) errors.push(`${where}: ${mid} was already started or is in a snapshot`);
        state.texts.add(mid);
        state.seenTexts.add(mid);
        break;
      }
      case "TEXT_MESSAGE_CONTENT":
        if (!state.texts.has(String(event.messageId))) errors.push(`${where}: content for a message that is not open`);
        break;
      case "TEXT_MESSAGE_END":
        if (!state.texts.has(String(event.messageId))) errors.push(`${where}: end for a message that is not open`);
        state.texts.delete(String(event.messageId));
        break;
      case "TOOL_CALL_START": {
        const tid = String(event.toolCallId);
        if (state.tools.has(tid) || state.seenTools.has(tid)) errors.push(`${where}: tool call ${tid} started twice`);
        const parent = event.parentMessageId;
        if (parent !== undefined && !state.seenTexts.has(String(parent))) {
          errors.push(`${where}: parentMessageId ${String(parent)} was never started`);
        }
        state.tools.add(tid);
        state.seenTools.add(tid);
        break;
      }
      case "TOOL_CALL_ARGS":
        if (!state.tools.has(String(event.toolCallId))) errors.push(`${where}: args for a call that is not open`);
        break;
      case "TOOL_CALL_END":
        if (!state.tools.has(String(event.toolCallId))) errors.push(`${where}: end for a call that is not open`);
        state.tools.delete(String(event.toolCallId));
        break;
      case "TOOL_CALL_RESULT":
        if (state.tools.has(String(event.toolCallId))) errors.push(`${where}: result before the call ended`);
        break;
      case "STEP_STARTED": {
        const name = String(event.stepName);
        if (state.steps.has(name)) errors.push(`${where}: step ${name} already open`);
        state.steps.add(name);
        break;
      }
      case "STEP_FINISHED":
        if (!state.steps.has(String(event.stepName))) errors.push(`${where}: step ${String(event.stepName)} not open`);
        state.steps.delete(String(event.stepName));
        break;
      case "RUN_FINISHED": {
        if (event.runId !== state.runId) errors.push(`${where}: runId differs from RUN_STARTED`);
        const open = [...state.texts, ...state.tools, ...state.steps];
        if (open.length) errors.push(`${where}: open items ${open.sort().join(",")}`);
        const outcome = (event.outcome as Record<string, unknown> | undefined) ?? {};
        for (const interrupt of (outcome.interrupts as Record<string, unknown>[] | undefined) ?? []) {
          const tid = interrupt.toolCallId;
          if (tid !== undefined && !state.seenTools.has(String(tid))) errors.push(`${where}: interrupt names unseen tool call ${String(tid)}`);
        }
        ended = true;
        break;
      }
      case "RUN_ERROR":
        ended = true;
        break;
    }
  });
  if (frames.some((f) => f.event) && !ended) errors.push("stream ended without RUN_FINISHED or RUN_ERROR");
  return errors;
}

function runIdOf(exchange: Exchange): string | undefined {
  const body = exchange.body as { runId?: string } | undefined;
  if (body?.runId) return body.runId;
  const match = exchange.url.match(/\/runs\/([^/]+)\/stream/);
  return match ? decodeURIComponent(match[1]) : undefined;
}

/**
 * Every conformance problem across a wire's exchanges: schema errors for each event and POST body,
 * and lifecycle errors per stream. A cut stream is checked only up to the cut (it may end open);
 * a stream that does not begin with RUN_STARTED is checked as a continuation of the same run's
 * earlier streams on this wire (their tool calls and still-open steps carried over).
 */
export function conformanceErrors(wire: Wire): string[] {
  const errors: string[] = [];
  const seenTools = new Map<string, Set<string>>();
  const openSteps = new Map<string, Set<string>>();
  wire.exchanges.forEach((exchange, n) => {
    const label = `exchange ${n} ${exchange.method} ${exchange.url.replace(/^.*\/api\/v1/, "")}`;
    if (exchange.method === "POST" && exchange.body !== undefined) {
      const parsed = RunAgentInputSchema.safeParse(exchange.body);
      if (!parsed.success) errors.push(`${label}: request body: ${parsed.error.message.slice(0, 300)}`);
    }
    if (exchange.status !== 200) return;
    eventsOf(exchange).forEach((event, i) => {
      const parsed = EventSchema.safeParse(event);
      if (!parsed.success) errors.push(`${label}: event #${i} ${String(event.type)}: ${parsed.error.message.slice(0, 300)}`);
    });
    const runId = runIdOf(exchange);
    const key = runId ?? "";
    const first = exchange.frames.find((f) => f.event)?.event;
    const continuation = first !== undefined && first.type !== "RUN_STARTED";
    const tools = seenTools.get(key) ?? new Set<string>();
    const steps = openSteps.get(key) ?? new Set<string>();
    const problems = lifecycleErrors(exchange.frames, {
      continuation,
      runId,
      knownToolCalls: continuation ? tools : [],
      knownOpenSteps: continuation ? steps : [],
    }).filter((p) => !(exchange.cut && p === "stream ended without RUN_FINISHED or RUN_ERROR"));
    errors.push(...problems.map((p) => `${label}: ${p}`));
    for (const event of eventsOf(exchange)) {
      if (event.type === "TOOL_CALL_START") tools.add(String(event.toolCallId));
      if (event.type === "STEP_STARTED") steps.add(String(event.stepName));
      if (event.type === "STEP_FINISHED") steps.delete(String(event.stepName));
    }
    seenTools.set(key, tools);
    openSteps.set(key, steps);
  });
  return errors;
}
