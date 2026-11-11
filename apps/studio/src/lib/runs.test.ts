import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { ApiError, getRun, subscribeRun, type RunEvent, type RunDelta, type RunStatusFrame } from "./runs";

// Frames exactly as apps/api/src/eap_api/sse.py writes them.
const enc = new TextEncoder();

function runEvent(seq: number, type: RunEvent["type"], data: Record<string, unknown> = {}): string {
  const body = JSON.stringify({ seq, type, data, ts: "2026-11-11T00:00:00+00:00" });
  return `id: ${seq}\nevent: run_event\ndata: ${body}\n\n`;
}

function statusFrame(status: string, display_status = status): string {
  return `event: status\ndata: ${JSON.stringify({ run_id: "r", status, display_status })}\n\n`;
}

function deltaFrame(delta: Partial<RunDelta>): string {
  return `event: delta\ndata: ${JSON.stringify(delta)}\n\n`;
}

interface Conn {
  url: string;
  headers: Record<string, string>;
  signal: AbortSignal;
  push(chunk: string | Uint8Array): void;
  close(): void;
  drop(): void;
}

/** fetch mock: every call opens a byte stream the test controls, served through a real Response. */
function installFetch() {
  const conns: Conn[] = [];
  const fetchMock = vi.fn(async (url: string | URL, init: RequestInit = {}) => {
    let ctrl!: ReadableStreamDefaultController<Uint8Array>;
    const body = new ReadableStream<Uint8Array>({
      start(c) {
        ctrl = c;
      },
    });
    const signal = init.signal as AbortSignal;
    signal.addEventListener("abort", () => {
      try {
        ctrl.error(new DOMException("The operation was aborted.", "AbortError"));
      } catch {
        // already closed
      }
    });
    conns.push({
      url: String(url),
      headers: { ...(init.headers as Record<string, string>) },
      signal,
      push: (chunk) => ctrl.enqueue(typeof chunk === "string" ? enc.encode(chunk) : chunk),
      close: () => ctrl.close(),
      drop: () => ctrl.error(new TypeError("network error")),
    });
    return new Response(body, { status: 200, headers: { "Content-Type": "text/event-stream" } });
  });
  vi.stubGlobal("fetch", fetchMock);
  return { fetchMock, conns };
}

/** Let stream reads and promise chains run (setImmediate is not faked). */
async function settle() {
  for (let i = 0; i < 20; i++) await new Promise((r) => setImmediate(r));
}

const BASE = 100;
const MAX = 800;

function collect() {
  const events: RunEvent[] = [];
  const deltas: RunDelta[] = [];
  const statuses: RunStatusFrame[] = [];
  const ends: string[] = [];
  return {
    events,
    deltas,
    statuses,
    ends,
    handlers: {
      onEvent: (e: RunEvent) => events.push(e),
      onDelta: (d: RunDelta) => deltas.push(d),
      onStatus: (s: RunStatusFrame) => statuses.push(s),
      onEnd: (r: string) => ends.push(r),
    },
  };
}

describe("subscribeRun", () => {
  beforeEach(() => {
    process.env.NEXT_PUBLIC_EAP_DEV_TOKEN = "Bearer dev:acme:alice";
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
  });

  afterEach(() => {
    delete process.env.NEXT_PUBLIC_EAP_DEV_TOKEN;
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  it("does not deliver a replayed event with seq <= lastSeq twice", async () => {
    const { conns } = installFetch();
    const c = collect();
    const unsubscribe = subscribeRun("r1", c.handlers, { baseDelayMs: BASE, maxDelayMs: MAX });
    await settle();

    conns[0].push(runEvent(0, "run_started") + runEvent(1, "model_message", { model_request: 0 }));
    conns[0].push(runEvent(2, "tool_call") + runEvent(3, "tool_result"));
    await settle();
    conns[0].drop();
    await settle();
    await vi.advanceTimersByTimeAsync(BASE);
    await settle();

    expect(c.events.map((e) => e.seq)).toEqual([0, 1, 2, 3]);
    // A server (or proxy) that replays overlapping events: 2 and 3 again, then new ones.
    expect(conns).toHaveLength(2);
    expect(conns[1].headers["Last-Event-ID"]).toBe("3");
    conns[1].push(statusFrame("running") + runEvent(2, "tool_call") + runEvent(3, "tool_result"));
    conns[1].push(runEvent(4, "model_message", { model_request: 1 }) + runEvent(5, "run_completed"));
    conns[1].close();
    await settle();

    expect(c.events.map((e) => e.seq)).toEqual([0, 1, 2, 3, 4, 5]);
    expect(c.ends).toEqual(["run_completed"]);
    unsubscribe();
  });

  it("after a drop following seq 3, the next fetch sends Last-Event-ID: 3", async () => {
    const { conns } = installFetch();
    const c = collect();
    const unsubscribe = subscribeRun("r1", c.handlers, { baseDelayMs: BASE, maxDelayMs: MAX });
    await settle();

    conns[0].push(statusFrame("running"));
    conns[0].push(runEvent(0, "run_started") + runEvent(1, "tool_call") + runEvent(2, "tool_result"));
    conns[0].push(runEvent(3, "model_message", { model_request: 1 }));
    await settle();
    conns[0].drop();
    await settle();
    await vi.advanceTimersByTimeAsync(BASE);
    await settle();

    expect(conns).toHaveLength(2);
    expect(conns[1].headers["Last-Event-ID"]).toBe("3");
    expect(conns[1].url).toBe("http://localhost:8000/api/v1/runs/r1/stream");
    unsubscribe();
  });

  it("reconnects after a clean close before the terminal event, then stops after run_completed", async () => {
    const { fetchMock, conns } = installFetch();
    const c = collect();
    subscribeRun("r1", c.handlers, { baseDelayMs: BASE, maxDelayMs: MAX });
    await settle();

    conns[0].push(statusFrame("running") + runEvent(0, "run_started") + runEvent(1, "model_message", { model_request: 0 }));
    conns[0].close();
    await settle();

    // Not before the backoff delay...
    await vi.advanceTimersByTimeAsync(BASE - 1);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    // ...but right after it.
    await vi.advanceTimersByTimeAsync(1);
    await settle();
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(conns[1].headers["Last-Event-ID"]).toBe("1");

    conns[1].push(runEvent(2, "run_completed", { output: "done" }));
    await settle();
    expect(c.ends).toEqual(["run_completed"]);
    expect(conns[1].signal.aborted).toBe(false);

    await vi.advanceTimersByTimeAsync(60_000);
    await settle();
    expect(fetchMock).toHaveBeenCalledTimes(2);
    expect(c.events.map((e) => [e.seq, e.type])).toEqual([
      [0, "run_started"],
      [1, "model_message"],
      [2, "run_completed"],
    ]);
  });

  it("backs off exponentially, capped, while connections close without progress", async () => {
    const { fetchMock, conns } = installFetch();
    subscribeRun("r1", {}, { baseDelayMs: BASE, maxDelayMs: 4 * BASE });
    await settle();

    const gaps: number[] = [];
    for (let i = 0; i < 5; i++) {
      conns[i].push(statusFrame("running"));
      conns[i].close();
      await settle();
      let waited = 0;
      while (fetchMock.mock.calls.length === i + 1) {
        await vi.advanceTimersByTimeAsync(BASE / 4);
        await settle();
        waited += BASE / 4;
      }
      gaps.push(waited);
    }
    expect(gaps).toEqual([BASE, 2 * BASE, 4 * BASE, 4 * BASE, 4 * BASE]);
  });

  it("unsubscribe aborts the in-flight fetch signal and no further fetch happens", async () => {
    const { fetchMock, conns } = installFetch();
    const c = collect();
    const unsubscribe = subscribeRun("r1", c.handlers, { baseDelayMs: BASE, maxDelayMs: MAX });
    await settle();
    conns[0].push(runEvent(0, "run_started"));
    await settle();

    expect(conns[0].signal.aborted).toBe(false);
    unsubscribe();
    expect(conns[0].signal.aborted).toBe(true);

    await vi.advanceTimersByTimeAsync(60_000);
    await settle();
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(c.events.map((e) => e.seq)).toEqual([0]);
    expect(c.ends).toEqual([]);
  });

  it("unsubscribe during the backoff wait prevents the reconnect", async () => {
    const { fetchMock, conns } = installFetch();
    const unsubscribe = subscribeRun("r1", {}, { baseDelayMs: BASE, maxDelayMs: MAX });
    await settle();
    await settle();
    conns[0].drop();
    await settle();

    unsubscribe();
    await vi.advanceTimersByTimeAsync(60_000);
    await settle();
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("keeps separate state for two concurrent subscriptions to different runs", async () => {
    const { conns } = installFetch();
    const a = collect();
    const b = collect();
    const unA = subscribeRun("run-a", a.handlers, { baseDelayMs: BASE, maxDelayMs: MAX });
    const unB = subscribeRun("run-b", b.handlers, { baseDelayMs: BASE, maxDelayMs: MAX });
    await settle();

    const conn = (run: string, n: number) => conns.filter((x) => x.url.includes(`/runs/${run}/`))[n];
    conn("run-a", 0).push(runEvent(0, "run_started") + runEvent(1, "tool_call") + runEvent(2, "tool_result"));
    conn("run-b", 0).push(runEvent(0, "run_started"));
    await settle();
    conn("run-a", 0).drop();
    conn("run-b", 0).drop();
    await settle();
    await vi.advanceTimersByTimeAsync(BASE);
    await settle();

    expect(conn("run-a", 1).headers["Last-Event-ID"]).toBe("2");
    expect(conn("run-b", 1).headers["Last-Event-ID"]).toBe("0");

    // run-b's seq 1 is new for run-b even though run-a has already seen seq 1 and 2.
    conn("run-b", 1).push(runEvent(1, "run_failed", { error: "boom" }));
    await settle();
    expect(b.events.map((e) => e.seq)).toEqual([0, 1]);
    expect(b.ends).toEqual(["run_failed"]);
    expect(a.events.map((e) => e.seq)).toEqual([0, 1, 2]);
    expect(a.ends).toEqual([]);

    unB();
    expect(conn("run-a", 1).signal.aborted).toBe(false);
    unA();
    expect(conn("run-a", 1).signal.aborted).toBe(true);
  });

  it("sends no Last-Event-ID on first connect, and Last-Event-ID: 0 after only seq 0", async () => {
    const { conns } = installFetch();
    const unsubscribe = subscribeRun("r1", {}, { baseDelayMs: BASE, maxDelayMs: MAX });
    await settle();

    expect(conns[0].headers).not.toHaveProperty("Last-Event-ID");
    conns[0].push(statusFrame("running") + runEvent(0, "run_started"));
    await settle();
    conns[0].drop();
    await settle();
    await vi.advanceTimersByTimeAsync(BASE);
    await settle();

    expect(conns[1].headers["Last-Event-ID"]).toBe("0");
    unsubscribe();
  });

  it("parses events split across byte chunks, including a split multi-byte UTF-8 character", async () => {
    const { conns } = installFetch();
    const c = collect();
    const unsubscribe = subscribeRun("r1", c.handlers, { baseDelayMs: BASE, maxDelayMs: MAX });
    await settle();

    const text = "Grüße ✓ 🎉 done";
    const payload =
      ": heartbeat\n\n" +
      statusFrame("running") +
      deltaFrame({ run_id: "r1", model_request: 0, attempt: "a1", delta: "Grü" }) +
      runEvent(0, "model_message", { model_request: 0, text });
    const bytes = enc.encode(payload);
    // Find the 4-byte emoji and cut through its middle, then feed the rest a few bytes at a time.
    const emojiAt = bytes.findIndex((b) => b === 0xf0);
    expect(emojiAt).toBeGreaterThan(0);
    const cuts = [emojiAt + 2];
    for (let i = 3; i < bytes.length; i += 5) cuts.push(i);
    cuts.sort((x, y) => x - y);
    let prev = 0;
    for (const cut of [...new Set(cuts), bytes.length]) {
      if (cut <= prev) continue;
      conns[0].push(bytes.slice(prev, cut));
      prev = cut;
      await settle();
    }
    await settle();

    expect(c.statuses).toEqual([{ run_id: "r", status: "running", display_status: "running" }]);
    expect(c.deltas).toEqual([{ run_id: "r1", model_request: 0, attempt: "a1", delta: "Grü" }]);
    expect(c.events).toHaveLength(1);
    expect(c.events[0].seq).toBe(0);
    expect((c.events[0].data as { text: string }).text).toBe(text);
    unsubscribe();
  });

  it("delivers an event as soon as its frame is complete, before the stream closes", async () => {
    const { conns } = installFetch();
    const c = collect();
    const unsubscribe = subscribeRun("r1", c.handlers, { baseDelayMs: BASE, maxDelayMs: MAX });
    await settle();

    const frame = runEvent(0, "run_started");
    conns[0].push(frame.slice(0, -1));
    await settle();
    expect(c.events).toHaveLength(0);
    conns[0].push("\n");
    await settle();
    expect(c.events.map((e) => e.seq)).toEqual([0]);
    unsubscribe();
  });

  it("stops for good on 404", async () => {
    const fetchMock = vi.fn(async () => new Response('{"detail":"run not found"}', { status: 404 }));
    vi.stubGlobal("fetch", fetchMock);
    const c = collect();
    subscribeRun("missing", c.handlers, { baseDelayMs: BASE, maxDelayMs: MAX });
    await settle();
    await vi.advanceTimersByTimeAsync(60_000);
    await settle();
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(c.ends).toEqual(["not_found"]);
  });

  it("stops when resuming after the final event: terminal status, then a clean close", async () => {
    const { fetchMock, conns } = installFetch();
    const c = collect();
    subscribeRun("r1", c.handlers, { initialLastSeq: 7, baseDelayMs: BASE, maxDelayMs: MAX });
    await settle();
    expect(conns[0].headers["Last-Event-ID"]).toBe("7");

    conns[0].push(statusFrame("completed"));
    conns[0].close();
    await settle();
    await vi.advanceTimersByTimeAsync(60_000);
    await settle();
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(c.ends).toEqual(["terminal_status"]);
  });
});

describe("getRun", () => {
  beforeEach(() => {
    process.env.NEXT_PUBLIC_EAP_DEV_TOKEN = "Bearer dev:acme:alice";
  });

  afterEach(() => {
    delete process.env.NEXT_PUBLIC_EAP_DEV_TOKEN;
    vi.unstubAllGlobals();
  });

  it("returns the API wire shape with run_id", async () => {
    const wireRun = {
      run_id: "0593b408-e845-4b31-9874-cae982e81b4e",
      agent_id: "853b7576-7cca-49af-a396-97089a390396",
      version: 1,
      status: "completed",
      display_status: "completed",
      input: "Customers in Europe see prices in USD instead of EUR on checkout.",
      output:
        "\n\nI've created a ticket for this issue: **TCK-6E5549094063**. Our team will look into the currency display problem for European customers.",
      error: null,
      heartbeat_at: "2026-11-11T17:38:15.926448+00:00",
      created_at: "2026-11-11T17:38:05.795192+00:00",
      updated_at: "2026-11-11T17:38:18.962035+00:00",
    };
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify(wireRun), { status: 200 })));

    await expect(getRun(wireRun.run_id)).resolves.toEqual(wireRun);
  });

  it("throws ApiError carrying the response status", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response(JSON.stringify({ detail: "Run not found" }), { status: 404 }))
    );

    await expect(getRun("missing")).rejects.toEqual(
      expect.objectContaining<ApiError>({ status: 404, message: "Run not found" })
    );
  });
});
