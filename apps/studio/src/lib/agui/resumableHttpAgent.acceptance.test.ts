import { EventType, type BaseEvent, type RunAgentInput } from "@ag-ui/client";
import { lastValueFrom, toArray } from "rxjs";
import { afterEach, describe, expect, it, vi } from "vitest";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import {
  BACKOFF_MAX_MS,
  RESUME_FAILED_CODE,
  ResumableHttpAgent,
  backoffDelayMs,
  lastEventIdKey,
} from "./resumableHttpAgent";
import { SseFrameParser, type SseFrame } from "./sseFrames";

const URL = "http://api.test/agents/a1";
const THREAD = "t1";
const RUN = "r1";
const enc = new TextEncoder();

function parseAll(chunks: (Uint8Array | string)[]): SseFrame[] {
  const parser = new SseFrameParser();
  const frames = chunks.flatMap((chunk) => parser.push(chunk));
  return [...frames, ...parser.end()];
}

describe("SSE parsing (acceptance)", () => {
  const text =
    ': keepalive\r\nid: r1:0\r\ndata: {"t":"héllo ✓ 😀"}\r\n\r\n' +
    "id: r1:1\ndata: a\ndata: b\n\n:ping\n\n" +
    "data: only\r\rdata: cr-only\r\r";
  const bytes = enc.encode(text);
  const expected: SseFrame[] = [
    { id: "r1:0", data: '{"t":"héllo ✓ 😀"}' },
    { id: "r1:1", data: "a\nb" },
    { data: "only" },
    { data: "cr-only" },
  ];

  it("one shot matches the expected frames (CRLF, LF, CR, comments, multi-line data)", () => {
    expect(parseAll([bytes])).toEqual(expected);
  });

  it("is identical when split at every byte boundary (multi-byte UTF-8 split)", () => {
    for (let cut = 0; cut <= bytes.length; cut += 1) {
      expect(parseAll([bytes.slice(0, cut), bytes.slice(cut)]), `cut ${cut}`).toEqual(expected);
    }
  });

  it("is identical when fed one byte at a time", () => {
    expect(parseAll(Array.from(bytes, (b) => Uint8Array.of(b)))).toEqual(expected);
  });

  it("tracks the id per frame and does not leak it into the next frame", () => {
    const frames = parseAll(["id: a:1\ndata: x\n\ndata: y\n\nid: a:2\ndata: z\n\n"]);
    expect(frames.map((f) => f.id)).toEqual(["a:1", undefined, "a:2"]);
  });

  it("ignores comment-only blocks and drops an unterminated trailing frame", () => {
    expect(parseAll([": hb\n\n: hb2\n\n"])).toEqual([]);
    expect(parseAll(["id: r1:9\ndata: {}\n"])).toEqual([]);
  });

  it("drops one space after the colon only, and ignores an id with NUL", () => {
    expect(parseAll(["data:  two\n\n"])[0]?.data).toBe(" two");
    expect(parseAll(["data:nospace\n\n"])[0]?.data).toBe("nospace");
    expect(parseAll(["id: bad\0id\ndata: x\n\n"])[0]?.id).toBeUndefined();
  });
});

// ---- mock server -------------------------------------------------------------------------

type Ev = Record<string, unknown>;
interface F {
  e: Ev;
  seq: number;
}
const ev = (type: string, rest: Ev = {}): Ev => ({ type, ...rest });
/** Every frame carries id r1:<seq>. */
const SERVER: F[] = [
  ev(EventType.RUN_STARTED, { threadId: THREAD, runId: RUN }),
  ev(EventType.TEXT_MESSAGE_START, { messageId: "m", role: "assistant" }),
  ev(EventType.TEXT_MESSAGE_CONTENT, { messageId: "m", delta: "a" }),
  ev(EventType.TOOL_CALL_START, { toolCallId: "c", toolCallName: "x" }),
  ev(EventType.TEXT_MESSAGE_CONTENT, { messageId: "m", delta: "b" }),
  ev(EventType.TOOL_CALL_END, { toolCallId: "c" }),
  ev(EventType.TEXT_MESSAGE_END, { messageId: "m" }),
  ev(EventType.RUN_FINISHED, { threadId: THREAD, runId: RUN }),
].map((e, seq) => ({ e, seq }));

const frameText = (f: F) => `id: ${RUN}:${f.seq}\ndata: ${JSON.stringify(f.e)}\n\n`;

interface Req {
  url: string;
  method: string;
  lastEventId: string | null;
  body?: string;
}
type Handler = (req: Req, init: RequestInit) => Response | Promise<Response>;

function bodyOf(text: string, end: "close" | "error" | "hang", signal?: AbortSignal) {
  let sent = false;
  return new ReadableStream<Uint8Array>({
    pull(c) {
      if (!sent) {
        sent = true;
        if (text) c.enqueue(enc.encode(text));
        return;
      }
      if (end === "close") c.close();
      else if (end === "error") c.error(new TypeError("network error"));
      else
        return new Promise<void>((_, reject) =>
          signal?.addEventListener("abort", () => reject(new DOMException("a", "AbortError"))),
        );
    },
  });
}

/** Mimics the server: a request resumes after the seq in Last-Event-ID, cut after `limit` frames. */
function replay(cut: number | null, end: "close" | "error" = "error"): Handler {
  return (req) => {
    const after = req.lastEventId ? Number(req.lastEventId.split(":").pop()) : -1;
    let frames = SERVER.filter((f) => f.seq > after);
    if (cut !== null) frames = frames.slice(0, cut);
    return new Response(bodyOf(frames.map(frameText).join(""), cut === null ? "close" : end), { status: 200 });
  };
}

function memoryStorage(): Storage {
  const m = new Map<string, string>();
  return {
    get length() {
      return m.size;
    },
    clear: () => m.clear(),
    getItem: (k) => m.get(k) ?? null,
    key: (i) => [...m.keys()][i] ?? null,
    removeItem: (k) => void m.delete(k),
    setItem: (k, v) => void m.set(k, v),
  };
}

function setup(handlers: Handler[], extra: Record<string, unknown> = {}) {
  const requests: Req[] = [];
  const delays: number[] = [];
  const fetch = (async (url: string, init: RequestInit) => {
    const req: Req = {
      url,
      method: init.method ?? "GET",
      lastEventId: new Headers(init.headers).get("Last-Event-ID"),
      body: typeof init.body === "string" ? init.body : undefined,
    };
    requests.push(req);
    const h = handlers[requests.length - 1] ?? handlers[handlers.length - 1]!;
    return h(req, init);
  }) as typeof globalThis.fetch;
  const storage = memoryStorage();
  const agent = new ResumableHttpAgent({
    url: URL,
    threadId: THREAD,
    fetch,
    storage,
    random: () => 0.5,
    sleep: async (ms) => void delays.push(ms),
    ...extra,
  });
  return { agent, requests, delays, storage };
}

const input = (): RunAgentInput => ({
  threadId: THREAD,
  runId: RUN,
  messages: [{ id: "u", role: "user", content: "hi" }],
  tools: [],
  context: [],
  state: {},
  forwardedProps: {},
});
const collect = (a: ResumableHttpAgent) => lastValueFrom(a.run(input()).pipe(toArray()));
const types = (evs: BaseEvent[]) => evs.map((e) => e.type);

afterEach(() => {
  vi.useRealTimers();
});

describe("resume and synthetic closes (acceptance)", () => {
  it("re-POSTs with the last event id; each server event arrives exactly once", async () => {
    const { agent, requests } = setup([replay(3), replay(null)]);
    const events = await collect(agent);
    expect(requests.map((r) => [r.method, r.lastEventId])).toEqual([
      ["POST", null],
      ["POST", `${RUN}:2`],
    ]);
    expect(requests[1]!.body).toBe(requests[0]!.body);
    const real = events.filter((e) => !(e as Ev).synthetic);
    const serverEvents = SERVER.map((f) => f.e.type);
    // the only extra event is the synthetic TEXT_MESSAGE_END for "m"
    expect(types(events).filter((t) => t === EventType.RUN_STARTED)).toHaveLength(1);
    expect(types(real).length).toBe(serverEvents.length + 1);
    expect(events[3]).toMatchObject({ type: EventType.TEXT_MESSAGE_END, messageId: "m" });
    expect(types(events.slice(0, 3))).toEqual(serverEvents.slice(0, 3));
    expect(types(events.slice(4))).toEqual(serverEvents.slice(3));
  });

  it("closes open messages first, then open tool calls, before re-POSTing", async () => {
    const { agent } = setup([replay(4), replay(null)]);
    const events = await collect(agent);
    expect(events.slice(4, 6)).toMatchObject([
      { type: EventType.TEXT_MESSAGE_END, messageId: "m" },
      { type: EventType.TOOL_CALL_END, toolCallId: "c" },
    ]);
    expect(events[6]).toMatchObject({ type: EventType.TEXT_MESSAGE_CONTENT, delta: "b" });
  });

  it("adds no synthetic close when nothing is open", async () => {
    const { agent } = setup([replay(1), replay(null)]);
    const events = await collect(agent);
    expect(types(events)).toEqual(SERVER.map((f) => f.e.type));
  });

  it("treats a clean EOF without a terminal event as a drop", async () => {
    const { agent, requests } = setup([replay(3, "close"), replay(null)]);
    const events = await collect(agent);
    expect(requests).toHaveLength(2);
    expect(events.at(-1)?.type).toBe(EventType.RUN_FINISHED);
  });

  it("stores the id while running and clears it after RUN_FINISHED", async () => {
    const { agent, storage } = setup([replay(3), replay(null)]);
    const sub = agent.run(input()).subscribe();
    await vi.waitFor(() => expect(sub.closed).toBe(true));
    expect(storage.getItem(lastEventIdKey(THREAD, RUN))).toBeNull();
    expect(agent.getActiveRunId(THREAD)).toBeNull();
  });

  it("keeps the stored id after the retry budget is exhausted", async () => {
    const boom: Handler = () => {
      throw new TypeError("down");
    };
    const { agent, storage } = setup([replay(3), boom]);
    const events = await collect(agent);
    expect(events.at(-1)).toMatchObject({ type: EventType.RUN_ERROR, code: RESUME_FAILED_CODE });
    expect(storage.getItem(lastEventIdKey(THREAD, RUN))).toBe(`${RUN}:2`);
  });
});

describe("backoff (acceptance)", () => {
  it("is jittered within ±20% and never exceeds the cap", () => {
    for (let attempt = 1; attempt <= 12; attempt += 1) {
      const base = Math.min(500 * 2 ** (attempt - 1), BACKOFF_MAX_MS);
      for (const r of [0, 0.25, 0.5, 0.75, 0.999999]) {
        const d = backoffDelayMs(attempt, () => r);
        expect(d).toBeGreaterThanOrEqual(Math.round(base * 0.8));
        expect(d).toBeLessThanOrEqual(Math.round(base * 1.2));
      }
    }
    expect(backoffDelayMs(50, () => 0.999999)).toBeLessThanOrEqual(BACKOFF_MAX_MS * 1.2);
    expect(new Set([0, 0.2, 0.9].map((r) => backoffDelayMs(3, () => r))).size).toBe(3);
  });

  it("waits 0.5,1,2,4,8 s over exactly 5 retries, then emits stream.resume_failed", async () => {
    const boom: Handler = () => {
      throw new TypeError("down");
    };
    const { agent, requests, delays } = setup([replay(1), boom]);
    const events = await collect(agent);
    expect(delays).toEqual([500, 1000, 2000, 4000, 8000]);
    expect(requests).toHaveLength(6);
    expect(events.at(-1)).toMatchObject({ type: EventType.RUN_ERROR, code: RESUME_FAILED_CODE });
  });

  it("holds the cap when more retries are allowed", async () => {
    const boom: Handler = () => {
      throw new TypeError("down");
    };
    const { agent, delays } = setup([replay(1), boom], { maxAttempts: 8 });
    await collect(agent);
    expect(delays).toEqual([500, 1000, 2000, 4000, 8000, 8000, 8000, 8000]);
  });

  it("errors the observable when nothing ever arrived", async () => {
    const boom: Handler = () => {
      throw new TypeError("down");
    };
    const { agent, requests } = setup([boom]);
    await expect(collect(agent)).rejects.toThrow(/after 5 retries/);
    expect(requests).toHaveLength(6);
  });

  it("abort cancels a pending retry and never re-fetches", async () => {
    let started!: () => void;
    const sleeping = new Promise<void>((r) => (started = r));
    const sleep = (_ms: number, signal: AbortSignal) =>
      new Promise<void>((resolve) => {
        started();
        signal.addEventListener("abort", () => resolve(), { once: true });
      });
    const { agent, requests } = setup([replay(3), replay(null)], { sleep });
    const seen: BaseEvent[] = [];
    let done = false;
    agent.run(input()).subscribe({ next: (e) => seen.push(e), complete: () => (done = true) });
    await sleeping;
    agent.abortRun();
    await vi.waitFor(() => expect(done).toBe(true));
    expect(requests).toHaveLength(1);
    expect(seen.at(-1)).toMatchObject({ type: EventType.RUN_ERROR, code: "abort" });
  });
});

describe("connect, concurrency, leaks (acceptance)", () => {
  it("connect() attaches through GET with the stored Last-Event-ID", async () => {
    const { agent, requests, storage } = setup([replay(null)]);
    storage.setItem(lastEventIdKey(THREAD, RUN), `${RUN}:4`);
    const connect = (agent as unknown as { connect(i: RunAgentInput): ReturnType<typeof agent.run> }).connect;
    const events = await lastValueFrom(connect.call(agent, input()).pipe(toArray()));
    expect(requests).toEqual([
      { url: `${URL}/threads/${THREAD}/runs/${RUN}/stream`, method: "GET", lastEventId: `${RUN}:4`, body: undefined },
    ]);
    expect(events[0]).toMatchObject({ type: EventType.RUN_STARTED, threadId: THREAD, runId: RUN });
    expect(types(events).slice(1)).toEqual(SERVER.slice(5).map((f) => f.e.type));
  });

  it("two agents on the same thread keep separate ids and storage", async () => {
    const a = setup([replay(3), replay(null)]);
    const b = setup([replay(5), replay(null)]);
    await Promise.all([collect(a.agent), collect(b.agent)]);
    expect(a.requests[1]!.lastEventId).toBe(`${RUN}:2`);
    expect(b.requests[1]!.lastEventId).toBe(`${RUN}:4`);
    expect(a.delays).toHaveLength(1);
    expect(b.delays).toHaveLength(1);
    expect(a.agent.getLastEventId(THREAD, RUN)).toBeNull();
  });

  function trackListeners(agent: ResumableHttpAgent) {
    const signal = agent.abortController.signal;
    const live = new Set<unknown>();
    const add = signal.addEventListener.bind(signal);
    const remove = signal.removeEventListener.bind(signal);
    signal.addEventListener = ((t: string, l: unknown, o?: unknown) => (
      live.add(l), add(t, l as EventListener, o as AddEventListenerOptions)
    )) as typeof signal.addEventListener;
    signal.removeEventListener = ((t: string, l: unknown, o?: unknown) => (
      live.delete(l), remove(t, l as EventListener, o as AddEventListenerOptions)
    )) as typeof signal.removeEventListener;
    return live;
  }

  it("leaves no timers or listeners after completion (default sleep, fake timers)", async () => {
    vi.useFakeTimers();
    const { agent } = setup([replay(3), replay(null)], { sleep: undefined });
    const live = trackListeners(agent);
    let done = false;
    agent.run(input()).subscribe({ complete: () => (done = true) });
    await vi.advanceTimersByTimeAsync(1000);
    expect(done).toBe(true);
    expect(vi.getTimerCount()).toBe(0);
    expect(live.size).toBe(0);
  });

  it("leaves no timers or listeners after an abort during backoff", async () => {
    vi.useFakeTimers();
    const { agent, requests } = setup([replay(3), replay(null)], { sleep: undefined });
    const live = trackListeners(agent);
    let done = false;
    agent.run(input()).subscribe({ complete: () => (done = true) });
    await vi.advanceTimersByTimeAsync(10);
    expect(vi.getTimerCount()).toBe(1);
    agent.abortRun();
    await vi.advanceTimersByTimeAsync(10);
    expect(done).toBe(true);
    expect(vi.getTimerCount()).toBe(0);
    expect(live.size).toBe(0);
    await vi.advanceTimersByTimeAsync(60_000);
    expect(requests).toHaveLength(1);
  });

  it("unsubscribing mid-backoff clears the timer and listeners", async () => {
    vi.useFakeTimers();
    const { agent, requests } = setup([replay(3), replay(null)], { sleep: undefined });
    const live = trackListeners(agent);
    const sub = agent.run(input()).subscribe();
    await vi.advanceTimersByTimeAsync(10);
    sub.unsubscribe();
    await vi.advanceTimersByTimeAsync(60_000);
    expect(vi.getTimerCount()).toBe(0);
    expect(live.size).toBe(0);
    expect(requests).toHaveLength(1);
  });
});

describe("dependency pins (acceptance)", () => {
  const root = resolve(__dirname, "../../../../..");
  const pkg = JSON.parse(readFileSync(resolve(root, "apps/studio/package.json"), "utf8"));
  const lock = readFileSync(resolve(root, "pnpm-lock.yaml"), "utf8");

  it("package.json pins exact versions", () => {
    expect(pkg.dependencies["@ag-ui/client"]).toBe("1.0.5");
    expect(pkg.dependencies["@ag-ui/core"]).toBe("1.0.5");
    expect(pkg.dependencies.rxjs).toBe("7.8.1");
  });

  it("the studio importer in the lockfile resolves those exact versions", () => {
    const studio = lock.slice(lock.indexOf("\n  apps/studio:"));
    for (const [name, version] of [
      ["'@ag-ui/client'", "1.0.5"],
      ["'@ag-ui/core'", "1.0.5"],
      ["rxjs", "7.8.1"],
    ]) {
      const m = new RegExp(`\\n      ${name}:\\n        specifier: ${version.replace(/\./g, "\\.")}\\n        version: ${version.replace(/\./g, "\\.")}`).exec(
        studio,
      );
      expect(m, name).not.toBeNull();
    }
  });

  it("the lockfile has one copy of each @ag-ui package and no other rxjs 7.8.x drift", () => {
    for (const name of ["client", "core", "encoder", "proto"]) {
      const keys = lock.match(new RegExp(`^  '@ag-ui/${name}@[^']+':`, "gm")) ?? [];
      expect(new Set(keys.map((k) => k.replace(/(\(.*\))?':$/, ""))).size, name).toBe(1);
      expect(keys.every((k) => k.includes("@1.0.5"))).toBe(true);
    }
    expect(lock).not.toMatch(/^ {2}rxjs@7\.8\.(?!1:)/m);
  });
});
