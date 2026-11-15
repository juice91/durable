import { EventType, type BaseEvent, type Message } from "@ag-ui/client";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import {
  ACTIVE_RUN_KEY_PREFIX,
  AgUiHttpError,
  MAX_RETRY_AFTER_MS,
  STREAM_INTERRUPTED_CODE,
  retryAfterMs,
  BACKOFF_MAX_MS,
  RESUME_FAILED_CODE,
  ResumableHttpAgent,
  activeRunKey,
  backoffDelayMs,
  defaultAttachUrl,
  lastEventIdKey,
} from "./resumableHttpAgent";
import { MAX_FRAME_CHARS, SseFrameTooLargeError } from "./sseFrames";

const URL = "http://api.test/api/v1/ag-ui/agents/agent-1";
const THREAD = "thread-1";
const RUN = "run-1";
const USER: Message = { id: "u1", role: "user", content: "hi" };

interface Frame {
  event: Record<string, unknown>;
  id?: string;
}

/** The server's full stream for RUN (plan D7 frame ids: only the last frame of a durable event). */
const FRAMES: Frame[] = [
  { event: { type: EventType.RUN_STARTED, threadId: THREAD, runId: RUN } },
  { event: { type: EventType.CUSTOM, name: "pydanticos.run", value: { run_id: "native-1" } } },
  { event: { type: EventType.TEXT_MESSAGE_START, messageId: `${RUN}-m0`, role: "assistant" } },
  { event: { type: EventType.TEXT_MESSAGE_CONTENT, messageId: `${RUN}-m0`, delta: "Hel" } },
  { event: { type: EventType.TEXT_MESSAGE_CONTENT, messageId: `${RUN}-m0`, delta: "lo" } },
  { event: { type: EventType.TEXT_MESSAGE_END, messageId: `${RUN}-m0` }, id: `${RUN}:0` },
  {
    event: {
      type: EventType.TOOL_CALL_START,
      toolCallId: "c1",
      toolCallName: "search",
      parentMessageId: `${RUN}-m0`,
    },
  },
  { event: { type: EventType.TOOL_CALL_ARGS, toolCallId: "c1", delta: '{"q":"x"}' } },
  { event: { type: EventType.TOOL_CALL_END, toolCallId: "c1" }, id: `${RUN}:1` },
  {
    event: {
      type: EventType.TOOL_CALL_RESULT,
      messageId: `${RUN}-t-c1`,
      toolCallId: "c1",
      role: "tool",
      content: "found",
    },
    id: `${RUN}:2`,
  },
  { event: { type: EventType.TEXT_MESSAGE_START, messageId: `${RUN}-m1`, role: "assistant" } },
  { event: { type: EventType.TEXT_MESSAGE_CONTENT, messageId: `${RUN}-m1`, delta: "Do" } },
  { event: { type: EventType.TEXT_MESSAGE_CONTENT, messageId: `${RUN}-m1`, delta: "ne" } },
  { event: { type: EventType.TEXT_MESSAGE_END, messageId: `${RUN}-m1` }, id: `${RUN}:3` },
  {
    event: { type: EventType.RUN_FINISHED, threadId: THREAD, runId: RUN, outcome: { type: "success" } },
    id: `${RUN}:4`,
  },
];

/** Durable state through each seq, as the server's MESSAGES_SNAPSHOT would carry it. */
const ASSISTANT_M0: Message = {
  id: `${RUN}-m0`,
  role: "assistant",
  content: "Hello",
  toolCalls: [{ id: "c1", type: "function", function: { name: "search", arguments: '{"q":"x"}' } }],
};
const TOOL_C1: Message = { id: `${RUN}-t-c1`, role: "tool", toolCallId: "c1", content: "found" };
const SNAPSHOTS: Record<number, Message[]> = {
  [-1]: [USER],
  0: [USER, { id: `${RUN}-m0`, role: "assistant", content: "Hello" }],
  1: [USER, ASSISTANT_M0],
  2: [USER, ASSISTANT_M0, TOOL_C1],
  3: [USER, ASSISTANT_M0, TOOL_C1, { id: `${RUN}-m1`, role: "assistant", content: "Done" }],
  4: [USER, ASSISTANT_M0, TOOL_C1, { id: `${RUN}-m1`, role: "assistant", content: "Done" }],
};

function encodeFrame(frame: Frame): string {
  return `${frame.id ? `id: ${frame.id}\n` : ""}data: ${JSON.stringify(frame.event)}\n\n`;
}

/** How the mock answers one request. */
interface Reply {
  status?: number;
  /** Deliver this many frames, then fail (or hang / close, see below). Default: all. */
  frames?: number;
  /** After the frames: "error" (network drop), "close" (clean EOF, no terminal) or "hang". */
  then?: "error" | "close" | "hang";
  /** Extra bytes of the next frame to deliver before the cut (a partial frame). */
  partialBytes?: number;
  /** Byte chunk size (default: one chunk per frame). */
  chunkSize?: number;
  /** Reject the fetch itself (no response). */
  reject?: boolean;
  /** Frames sent after the first `frames` ones (then the stream closes). */
  append?: Frame[];
  /** Retry-After header of an error reply. */
  retryAfter?: string;
}

interface Request {
  url: string;
  method: string;
  lastEventId: string | null;
  body: string | undefined;
}

/** A mock AG-UI server: full stream without Last-Event-ID, continuation (D7) with it. */
function mockServer(replies: Reply[]) {
  const requests: Request[] = [];
  const fetch = async (url: string, init: RequestInit): Promise<Response> => {
    const headers = new Headers(init.headers);
    const lastEventId = headers.get("Last-Event-ID");
    requests.push({
      url,
      method: init.method ?? "GET",
      lastEventId,
      body: typeof init.body === "string" ? init.body : undefined,
    });
    const reply = replies[requests.length - 1] ?? {};
    if (reply.reject) throw new TypeError("Failed to fetch");
    if (reply.status && reply.status >= 400) {
      return new Response(JSON.stringify({ detail: "nope" }), {
        status: reply.status,
        headers: {
          "content-type": "application/json",
          ...(reply.retryAfter ? { "Retry-After": reply.retryAfter } : {}),
        },
      });
    }
    let frames: Frame[];
    if (lastEventId === null && (init.method ?? "GET") === "GET") {
      // attach from the start: a standalone stream with the thread history
      frames = [
        FRAMES[0],
        FRAMES[1],
        { event: { type: EventType.MESSAGES_SNAPSHOT, messages: SNAPSHOTS[-1] } },
        ...FRAMES.slice(2),
      ];
    } else if (lastEventId === null) {
      frames = FRAMES;
    } else {
      const seq = Number(lastEventId.slice(lastEventId.lastIndexOf(":") + 1));
      const after = seq === -1 ? 1 : FRAMES.findIndex((frame) => frame.id === lastEventId) + 1;
      frames = [
        { event: { type: EventType.CUSTOM, name: "pydanticos.run", value: { run_id: "native-1" } } },
        { event: { type: EventType.MESSAGES_SNAPSHOT, messages: SNAPSHOTS[seq] } },
        ...FRAMES.slice(after),
      ];
    }
    const count = reply.frames ?? frames.length;
    let text = [...frames.slice(0, count), ...(reply.append ?? [])].map(encodeFrame).join("");
    if (reply.partialBytes && frames[count]) text += encodeFrame(frames[count]).slice(0, reply.partialBytes);
    const bytes = new TextEncoder().encode(text);
    const size = reply.chunkSize ?? Math.max(bytes.length, 1);
    const then = reply.then ?? (count < frames.length && !reply.append ? "error" : "close");
    const signal = init.signal;
    const body = new ReadableStream<Uint8Array>({
      start(controller) {
        for (let index = 0; index < bytes.length; index += size) {
          controller.enqueue(bytes.slice(index, index + size));
        }
        const abort = () => {
          try {
            controller.error(new DOMException("The operation was aborted.", "AbortError"));
          } catch {
            // already closed
          }
        };
        if (signal?.aborted) abort();
        signal?.addEventListener("abort", abort);
        if (then === "close") controller.close();
        else if (then === "error") {
          // let the reader drain the enqueued chunks first
          setTimeout(() => {
            try {
              controller.error(new TypeError("network error"));
            } catch {
              // closed by abort
            }
          }, 0);
        }
      },
    });
    return new Response(body, { status: 200, headers: { "content-type": "text/event-stream" } });
  };
  return { fetch, requests };
}

function makeAgent(fetch: ReturnType<typeof mockServer>["fetch"], extra: Record<string, unknown> = {}) {
  const delays: number[] = [];
  const agent = new ResumableHttpAgent({
    url: URL,
    threadId: THREAD,
    initialMessages: [USER],
    fetch,
    sleep: async (ms) => {
      delays.push(ms);
    },
    random: () => 0.5,
    ...extra,
  });
  return { agent, delays };
}

async function runCollect(agent: ResumableHttpAgent) {
  const events: BaseEvent[] = [];
  let error: unknown;
  try {
    await agent.runAgent({ runId: RUN }, { onEvent: ({ event }) => void events.push(event) });
  } catch (caught) {
    error = caught;
  }
  return { events, error, messages: agent.messages };
}

/** The messages a run without any cut ends with. */
async function serverMessages(): Promise<Message[]> {
  const { agent } = makeAgent(mockServer([{}]).fetch);
  await agent.runAgent({ runId: RUN });
  return agent.messages;
}

beforeEach(() => window.sessionStorage.clear());
afterEach(() => window.sessionStorage.clear());

describe("backoffDelayMs", () => {
  it("doubles from 0.5 s to an 8 s cap with ±20% jitter", () => {
    expect([1, 2, 3, 4, 5, 6].map((n) => backoffDelayMs(n, () => 0.5))).toEqual([
      500, 1000, 2000, 4000, 8000, 8000,
    ]);
    for (let n = 1; n <= 6; n += 1) {
      const base = Math.min(500 * 2 ** (n - 1), BACKOFF_MAX_MS);
      expect(backoffDelayMs(n, () => 0)).toBe(Math.round(base * 0.8));
      expect(backoffDelayMs(n, () => 0.999999)).toBeLessThanOrEqual(Math.round(base * 1.2));
      expect(backoffDelayMs(n, () => 0.999999)).toBeGreaterThan(Math.round(base * 1.19));
    }
  });
});

describe("ResumableHttpAgent", () => {
  it("runs an uncut stream like HttpAgent and clears its stored ids at the end", async () => {
    const server = mockServer([{ chunkSize: 5 }]);
    const { agent } = makeAgent(server.fetch);
    const { events, error, messages } = await runCollect(agent);
    expect(error).toBeUndefined();
    expect(server.requests).toHaveLength(1);
    expect(server.requests[0].lastEventId).toBeNull();
    expect(server.requests[0].method).toBe("POST");
    expect(events.map((event) => event.type)).toEqual(FRAMES.map((frame) => frame.event.type));
    expect(messages).toEqual(SNAPSHOTS[4]);
    expect(window.sessionStorage.getItem(lastEventIdKey(THREAD, RUN))).toBeNull();
    expect(window.sessionStorage.getItem(activeRunKey(THREAD))).toBeNull();
  });

  it("tracks ids across chunk boundaries and resumes from the last fully read one", async () => {
    // frames 0..9 arrive (last id run-1:2) plus half of frame 10, in 3-byte chunks
    const server = mockServer([{ frames: 10, partialBytes: 20, chunkSize: 3 }, {}]);
    const { agent } = makeAgent(server.fetch);
    const { error, messages } = await runCollect(agent);
    expect(error).toBeUndefined();
    expect(server.requests.map((request) => request.lastEventId)).toEqual([null, `${RUN}:2`]);
    expect(messages).toEqual(await serverMessages());
  });

  it("resumes a cut in the middle of a text message: synthetic END, then the continuation", async () => {
    // cut after "Do" of the second message (frames 0..11)
    const server = mockServer([{ frames: 12 }, {}]);
    const { agent, delays } = makeAgent(server.fetch);
    const { events, error, messages } = await runCollect(agent);
    expect(error).toBeUndefined();
    expect(server.requests.map((request) => request.lastEventId)).toEqual([null, `${RUN}:2`]);
    // the identical body is re-POSTed to the same URL
    expect(server.requests[1].url).toBe(URL);
    expect(server.requests[1].method).toBe("POST");
    expect(server.requests[1].body).toBe(server.requests[0].body);
    const types = events.map((event) => event.type);
    const cut = types.indexOf(EventType.MESSAGES_SNAPSHOT);
    expect(events[cut - 2]).toMatchObject({ type: EventType.TEXT_MESSAGE_END, messageId: `${RUN}-m1` });
    expect(events[cut - 1]).toMatchObject({ type: EventType.CUSTOM, name: "pydanticos.run" });
    expect(types.filter((type) => type === EventType.RUN_STARTED)).toHaveLength(1);
    expect(types.at(-1)).toBe(EventType.RUN_FINISHED);
    expect(delays).toEqual([500]);
    expect(messages).toEqual(await serverMessages());
  });

  it("closes an open tool call locally before resuming", async () => {
    const server = mockServer([{ frames: 8 }, {}]);
    const { agent } = makeAgent(server.fetch);
    const { events, error, messages } = await runCollect(agent);
    expect(error).toBeUndefined();
    expect(server.requests[1].lastEventId).toBe(`${RUN}:0`);
    const synthetic = events.findIndex(
      (event, index) => index >= 8 && event.type === EventType.TOOL_CALL_END,
    );
    expect(events[synthetic]).toMatchObject({ toolCallId: "c1" });
    expect(events[synthetic + 1]).toMatchObject({ type: EventType.CUSTOM });
    expect(messages).toEqual(await serverMessages());
  });

  it("sends {runId}:-1 when only RUN_STARTED arrived", async () => {
    const server = mockServer([{ frames: 1 }, {}]);
    const { agent } = makeAgent(server.fetch);
    const { error, messages } = await runCollect(agent);
    expect(error).toBeUndefined();
    expect(server.requests.map((request) => request.lastEventId)).toEqual([null, `${RUN}:-1`]);
    expect(messages).toEqual(await serverMessages());
  });

  it("re-POSTs without Last-Event-ID when nothing arrived", async () => {
    const server = mockServer([{ reject: true }, { status: 503 }, {}]);
    const { agent, delays } = makeAgent(server.fetch);
    const { error, messages } = await runCollect(agent);
    expect(error).toBeUndefined();
    expect(server.requests.map((request) => request.lastEventId)).toEqual([null, null, null]);
    expect(delays).toEqual([500, 1000]);
    expect(messages).toEqual(await serverMessages());
  });

  it("resumes after a clean EOF without a terminal event", async () => {
    const server = mockServer([{ frames: 6, then: "close" }, {}]);
    const { agent } = makeAgent(server.fetch);
    const { error, messages } = await runCollect(agent);
    expect(error).toBeUndefined();
    expect(server.requests[1].lastEventId).toBe(`${RUN}:0`);
    expect(messages).toEqual(await serverMessages());
  });

  it("gives up after 5 retries with RUN_ERROR stream.resume_failed and backs off 0.5 s → 8 s", async () => {
    const server = mockServer([{ frames: 6 }, { reject: true }, { reject: true }, { reject: true }, { reject: true }, { reject: true }]);
    const { agent, delays } = makeAgent(server.fetch);
    const { events } = await runCollect(agent);
    expect(server.requests).toHaveLength(6);
    expect(delays).toEqual([500, 1000, 2000, 4000, 8000]);
    expect(events.at(-1)).toMatchObject({ type: EventType.RUN_ERROR, code: RESUME_FAILED_CODE });
    // the server run may still be alive: the id stays for a later connect()
    expect(window.sessionStorage.getItem(lastEventIdKey(THREAD, RUN))).toBe(`${RUN}:0`);
  });

  it("resets the retry budget after progress", async () => {
    // each attempt delivers one more durable event, then drops
    const server = mockServer([
      { frames: 6 },
      { reject: true },
      { frames: 4 }, // continuation from run-1:0: run, snapshot, START, ARGS
      { frames: 3 }, // continuation from run-1:0 again: run, snapshot, START → no new id but progress
      {},
    ]);
    const { agent, delays } = makeAgent(server.fetch);
    const { error, messages } = await runCollect(agent);
    expect(error).toBeUndefined();
    expect(delays).toEqual([500, 1000, 500, 500]);
    expect(messages).toEqual(await serverMessages());
  });

  it("stops on a 4xx while resuming with RUN_ERROR stream.resume_failed", async () => {
    const server = mockServer([{ frames: 6 }, { status: 404 }]);
    const { agent } = makeAgent(server.fetch);
    const { events } = await runCollect(agent);
    expect(server.requests).toHaveLength(2);
    expect(events.at(-1)).toMatchObject({ type: EventType.RUN_ERROR, code: RESUME_FAILED_CODE });
    expect(window.sessionStorage.getItem(lastEventIdKey(THREAD, RUN))).toBeNull();
  });

  it("fails like HttpAgent on a 4xx before anything arrived, without retrying", async () => {
    const server = mockServer([{ status: 409 }]);
    const { agent } = makeAgent(server.fetch);
    const { events, error } = await runCollect(agent);
    expect(server.requests).toHaveLength(1);
    expect(error).toBeInstanceOf(AgUiHttpError);
    expect(error).toMatchObject({ status: 409, payload: { detail: "nope" } });
    expect(events.some((event) => (event as { code?: string }).code === RESUME_FAILED_CODE)).toBe(false);
  });

  it("never retries after abortRun(); the stored id is kept for a later attach", async () => {
    const server = mockServer([{ frames: 6, then: "hang" }]);
    const { agent, delays } = makeAgent(server.fetch);
    const events: BaseEvent[] = [];
    const done = agent
      .runAgent(
        { runId: RUN },
        {
          onEvent: ({ event }) => {
            events.push(event);
            if (event.type === EventType.TEXT_MESSAGE_END) setTimeout(() => agent.abortRun(), 0);
          },
        },
      )
      .catch(() => undefined);
    await done;
    expect(server.requests).toHaveLength(1);
    expect(delays).toEqual([]);
    expect(events.at(-1)).toMatchObject({ type: EventType.RUN_ERROR, code: "abort" });
    expect(window.sessionStorage.getItem(lastEventIdKey(THREAD, RUN))).toBe(`${RUN}:0`);
    expect(window.sessionStorage.getItem(activeRunKey(THREAD))).toBe(RUN);
  });

  it("connect() GETs the attach route with the stored id and opens the run locally", async () => {
    window.sessionStorage.setItem(activeRunKey(THREAD), RUN);
    window.sessionStorage.setItem(lastEventIdKey(THREAD, RUN), `${RUN}:2`);
    const server = mockServer([{}]);
    const { agent } = makeAgent(server.fetch, { initialMessages: [] });
    const events: BaseEvent[] = [];
    await agent.connectAgent({}, { onEvent: ({ event }) => void events.push(event) });
    expect(server.requests).toEqual([
      {
        url: defaultAttachUrl(URL, THREAD, RUN),
        method: "GET",
        lastEventId: `${RUN}:2`,
        body: undefined,
      },
    ]);
    expect(server.requests[0].url).toBe(`${URL}/threads/${THREAD}/runs/${RUN}/stream`);
    expect(events[0]).toMatchObject({ type: EventType.RUN_STARTED, threadId: THREAD, runId: RUN });
    expect(agent.messages).toEqual(await serverMessages());
    expect(window.sessionStorage.getItem(activeRunKey(THREAD))).toBeNull();
  });

  it("connect() without a stored id attaches from the start; attachRunId wins", async () => {
    const server = mockServer([{}]);
    const { agent } = makeAgent(server.fetch, { initialMessages: [] });
    agent.attachRunId = RUN;
    await agent.connectAgent({ runId: "other" });
    expect(server.requests[0].url).toBe(defaultAttachUrl(URL, THREAD, RUN));
    expect(server.requests[0].lastEventId).toBeNull();
    expect(agent.messages).toEqual(await serverMessages());
  });

  it("connect() resumes a dropped attach with Last-Event-ID", async () => {
    const server = mockServer([{ frames: 10 }, {}]);
    const { agent } = makeAgent(server.fetch, { initialMessages: [] });
    agent.attachRunId = RUN;
    await agent.connectAgent();
    expect(agent.messages).toEqual(await serverMessages());
    expect(server.requests.map((request) => [request.method, request.lastEventId])).toEqual([
      ["GET", null],
      ["GET", `${RUN}:1`],
    ]);
  });

  it("tolerates storage that throws", async () => {
    const broken = {
      getItem: () => {
        throw new Error("blocked");
      },
      setItem: () => {
        throw new Error("quota");
      },
      removeItem: () => {
        throw new Error("blocked");
      },
    } as unknown as Storage;
    const server = mockServer([{ frames: 12 }, {}]);
    const { agent } = makeAgent(server.fetch, { storage: broken });
    const { error, messages } = await runCollect(agent);
    expect(error).toBeUndefined();
    expect(server.requests[1].lastEventId).toBe(`${RUN}:2`);
    expect(messages).toEqual(await serverMessages());
  });

  it("tolerates a storage getter that throws or is null", async () => {
    for (const storage of [
      () => {
        throw new Error("SecurityError");
      },
      null,
    ]) {
      const server = mockServer([{ frames: 12 }, {}]);
      const { agent } = makeAgent(server.fetch, { storage });
      const { error } = await runCollect(agent);
      expect(error).toBeUndefined();
      expect(server.requests[1].lastEventId).toBe(`${RUN}:2`);
      expect(agent.getLastEventId(THREAD, RUN)).toBeNull();
    }
    expect(Object.keys(window.sessionStorage).some((key) => key.startsWith(ACTIVE_RUN_KEY_PREFIX))).toBe(false);
  });

  it("clone() keeps the config and id state, and the copy runs and resumes", async () => {
    const server = mockServer([{ frames: 12 }, { status: 503 }, {}]);
    const delays: number[] = [];
    const attachUrl = (threadId: string, runId: string) => `http://other.test/${threadId}/${runId}`;
    const original = new ResumableHttpAgent({
      url: URL,
      threadId: THREAD,
      initialMessages: [USER],
      headers: { Authorization: "Bearer x" },
      fetch: server.fetch,
      storage: null,
      attachUrl,
      sleep: async (ms) => void delays.push(ms),
      random: () => 0,
      maxAttempts: 1,
      maxReconnects: 7,
    });
    original.attachRunId = RUN;
    const copy = original.clone();
    expect(copy).toBeInstanceOf(ResumableHttpAgent);
    expect(copy.url).toBe(URL);
    expect(copy.headers).toEqual({ Authorization: "Bearer x" });
    expect(copy.attachRunId).toBe(RUN);
    expect(copy.attachUrl(THREAD, RUN)).toBe(`http://other.test/${THREAD}/${RUN}`);
    expect(copy.getLastEventId(THREAD, RUN)).toBeNull();
    const events: BaseEvent[] = [];
    await copy.runAgent({ runId: RUN }, { onEvent: ({ event }) => void events.push(event) });
    // random 0 → -20% jitter; maxAttempts 1 is carried over (the default 5 would reach the 3rd reply)
    expect(delays).toEqual([400]);
    expect(server.requests.map((request) => request.lastEventId)).toEqual([null, `${RUN}:2`]);
    expect(events.at(-1)).toMatchObject({ type: EventType.RUN_ERROR, code: RESUME_FAILED_CODE });
    // id state is copied, not shared
    expect(original.getLastEventId(THREAD, RUN)).toBeNull();
  });

  it("clone() copies the in-memory ids of an unfinished run", async () => {
    const server = mockServer([{ frames: 6, then: "hang" }]);
    const { agent } = makeAgent(server.fetch, { storage: null });
    const run = agent
      .runAgent(
        { runId: RUN },
        { onEvent: ({ event }) => void (event.type === EventType.TEXT_MESSAGE_END && setTimeout(() => agent.abortRun(), 0)) },
      )
      .catch(() => undefined);
    await run;
    const copy = agent.clone();
    expect(copy.getLastEventId(THREAD, RUN)).toBe(`${RUN}:0`);
    expect(copy.getActiveRunId(THREAD)).toBe(RUN);
  });

  it("a continuation that only replays the snapshot is not progress: the budget runs out", async () => {
    // each resume delivers CUSTOM pydanticos.run + MESSAGES_SNAPSHOT, then drops
    const server = mockServer([{ frames: 6 }, ...Array.from({ length: 10 }, () => ({ frames: 2 }))]);
    const { agent, delays } = makeAgent(server.fetch);
    const { events } = await runCollect(agent);
    expect(server.requests).toHaveLength(6);
    expect(server.requests.slice(1).every((request) => request.lastEventId === `${RUN}:0`)).toBe(true);
    expect(delays).toEqual([500, 1000, 2000, 4000, 8000]);
    expect(events.at(-1)).toMatchObject({ type: EventType.RUN_ERROR, code: RESUME_FAILED_CODE });
  });

  it("bounds reconnects per run even when every attempt makes progress", async () => {
    // every attempt delivers new deltas (progress) but never a new id
    const server = mockServer(Array.from({ length: 40 }, () => ({ frames: 4 })));
    const { agent } = makeAgent(server.fetch, { maxReconnects: 9 });
    const { events } = await runCollect(agent);
    expect(server.requests).toHaveLength(10);
    expect(events.at(-1)).toMatchObject({ type: EventType.RUN_ERROR, code: RESUME_FAILED_CODE });
    expect((events.at(-1) as { message?: string }).message).toContain("9 reconnects");
  });

  it("resumes after RUN_ERROR stream_interrupted instead of ending the run", async () => {
    const interrupted: Frame = {
      event: { type: EventType.RUN_ERROR, code: STREAM_INTERRUPTED_CODE, message: "API failed" },
    };
    const server = mockServer([{ frames: 12, append: [interrupted] }, {}]);
    const { agent } = makeAgent(server.fetch);
    const { events, error, messages } = await runCollect(agent);
    expect(error).toBeUndefined();
    expect(server.requests.map((request) => request.lastEventId)).toEqual([null, `${RUN}:2`]);
    expect(events.some((event) => (event as { code?: string }).code === STREAM_INTERRUPTED_CODE)).toBe(false);
    expect(events.filter((event) => event.type === EventType.RUN_ERROR)).toHaveLength(0);
    expect(messages).toEqual(await serverMessages());
  });

  it("passes on any other server RUN_ERROR as the run's end", async () => {
    const failed: Frame = { event: { type: EventType.RUN_ERROR, code: "needs_attention", message: "x" } };
    const server = mockServer([{ frames: 6, append: [failed] }]);
    const { agent } = makeAgent(server.fetch);
    const { events } = await runCollect(agent);
    expect(server.requests).toHaveLength(1);
    expect(events.at(-1)).toMatchObject({ type: EventType.RUN_ERROR, code: "needs_attention" });
  });

  it("retries 429 and 408, waiting at least Retry-After", async () => {
    const server = mockServer([{ frames: 6 }, { status: 429, retryAfter: "3" }, { status: 408 }, {}]);
    const { agent, delays } = makeAgent(server.fetch);
    const { error, messages } = await runCollect(agent);
    expect(error).toBeUndefined();
    expect(server.requests).toHaveLength(4);
    expect(delays).toEqual([500, 3000, 2000]);
    expect(messages).toEqual(await serverMessages());
  });

  it("retries a 429 on the first request too", async () => {
    const server = mockServer([{ status: 429 }, {}]);
    const { agent } = makeAgent(server.fetch);
    const { error, messages } = await runCollect(agent);
    expect(error).toBeUndefined();
    expect(server.requests.map((request) => request.lastEventId)).toEqual([null, null]);
    expect(messages).toEqual(await serverMessages());
  });

  it("reads Retry-After as seconds or an HTTP date, capped", () => {
    expect(retryAfterMs(null)).toBeNull();
    expect(retryAfterMs("soon")).toBeNull();
    expect(retryAfterMs("2")).toBe(2000);
    expect(retryAfterMs("99999")).toBe(MAX_RETRY_AFTER_MS);
    const now = Date.parse("2026-11-15T10:00:00Z");
    expect(retryAfterMs("Sun, 15 Nov 2026 10:00:05 GMT", now)).toBe(5000);
    expect(retryAfterMs("Sun, 15 Nov 2026 09:00:00 GMT", now)).toBe(0);
  });

  it("connect() with no run to attach to completes without a request", async () => {
    const server = mockServer([{}]);
    const { agent } = makeAgent(server.fetch, { initialMessages: [] });
    const events: BaseEvent[] = [];
    await agent.connectAgent({}, { onEvent: ({ event }) => void events.push(event) });
    expect(server.requests).toHaveLength(0);
    expect(events).toEqual([]);
  });

  it("connect() completes quietly on a 404 and forgets the run", async () => {
    window.sessionStorage.setItem(activeRunKey(THREAD), RUN);
    window.sessionStorage.setItem(lastEventIdKey(THREAD, RUN), `${RUN}:2`);
    const server = mockServer([{ status: 404 }]);
    const { agent } = makeAgent(server.fetch, { initialMessages: [] });
    const events: BaseEvent[] = [];
    await agent.connectAgent({}, { onEvent: ({ event }) => void events.push(event) });
    expect(server.requests).toHaveLength(1);
    expect(events).toEqual([]);
    expect(window.sessionStorage.getItem(activeRunKey(THREAD))).toBeNull();
    expect(window.sessionStorage.getItem(lastEventIdKey(THREAD, RUN))).toBeNull();
  });

  it("fails a run whose frame exceeds the size cap, without retrying", async () => {
    const huge = "x".repeat(MAX_FRAME_CHARS + 10);
    const fetch = async () =>
      new Response(`data: {"type":"RUN_STARTED","threadId":"${THREAD}","runId":"${RUN}"}\n\ndata: ${huge}`, {
        status: 200,
        headers: { "content-type": "text/event-stream" },
      });
    const { agent, delays } = makeAgent(fetch);
    const { error } = await runCollect(agent);
    expect(error).toBeInstanceOf(SseFrameTooLargeError);
    expect(delays).toEqual([]);
  });
});

describe("ResumableHttpAgent workflow attach", () => {
  const WF_FRAMES = [
    { event: { type: EventType.CUSTOM, name: "pydanticos.run", value: { run_id: "wf-1" } } },
    { event: { type: EventType.STATE_SNAPSHOT, snapshot: { status: "running", steps: { a: { status: "running" } } } } },
    { event: { type: EventType.STEP_FINISHED, stepName: "a" }, id: `${RUN}:5` },
    { event: { type: EventType.STEP_STARTED, stepName: "b" }, id: `${RUN}:6` },
    { event: { type: EventType.STEP_FINISHED, stepName: "b" }, id: `${RUN}:7` },
    {
      event: { type: EventType.RUN_FINISHED, threadId: THREAD, runId: RUN, outcome: { type: "success" } },
      id: `${RUN}:8`,
    },
  ];

  it("a continuation opens a step that started before Last-Event-ID", async () => {
    window.sessionStorage.setItem(activeRunKey(THREAD), RUN);
    window.sessionStorage.setItem(lastEventIdKey(THREAD, RUN), `${RUN}:4`);
    const fetch = async () =>
      new Response(WF_FRAMES.map(encodeFrame).join(""), {
        status: 200,
        headers: { "content-type": "text/event-stream" },
      });
    const { agent } = makeAgent(fetch, { initialMessages: [] });
    const events: BaseEvent[] = [];
    await agent.connectAgent({}, { onEvent: ({ event }) => void events.push(event) });
    expect(events.map((event) => [event.type, (event as { stepName?: string }).stepName])).toEqual([
      [EventType.RUN_STARTED, undefined],
      [EventType.CUSTOM, undefined],
      [EventType.STATE_SNAPSHOT, undefined],
      [EventType.STEP_STARTED, "a"],
      [EventType.STEP_FINISHED, "a"],
      [EventType.STEP_STARTED, "b"],
      [EventType.STEP_FINISHED, "b"],
      [EventType.RUN_FINISHED, undefined],
    ]);
  });
});

