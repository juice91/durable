import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { parseSSE } from "./sse";

describe("SSE parser", () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("handles a single complete event", async () => {
    const chunks = [
      "event: run_event\n",
      "id: 1\n",
      "data: {\"type\":\"run_started\",\"ts\":\"2026-11-11T00:00:00Z\"}\n",
      "\n",
    ];

    const result: any[] = [];
    for await (const ev of parseSSE(chunks.join(""))) {
      result.push(ev);
    }

    expect(result).toHaveLength(1);
    expect(result[0]).toEqual({
      event: "run_event",
      id: "1",
      data: { type: "run_started", ts: "2026-11-11T00:00:00Z" },
    });
  });

  it("ignores comment lines starting with colon", async () => {
    const chunks = [
      ": heartbeat\n",
      "event: run_event\n",
      "id: 1\n",
      "data: {\"type\":\"run_started\"}\n",
      "\n",
    ];

    const result: any[] = [];
    for await (const ev of parseSSE(chunks.join(""))) {
      result.push(ev);
    }

    expect(result).toHaveLength(1);
    expect(result[0].event).toBe("run_event");
  });

  it("handles multiple events in one chunk", async () => {
    const chunk =
      "event: run_event\n" +
      "id: 1\n" +
      "data: {\"type\":\"run_started\"}\n" +
      "\n" +
      "event: run_event\n" +
      "id: 2\n" +
      "data: {\"type\":\"model_message\"}\n" +
      "\n";

    const result: any[] = [];
    for await (const ev of parseSSE(chunk)) {
      result.push(ev);
    }

    expect(result).toHaveLength(2);
    expect(result[0].id).toBe("1");
    expect(result[1].id).toBe("2");
  });

  it("splits events across chunks correctly", async () => {
    const chunks = [
      "event: run_event\nid: 1\ndata: {\"type\":\"run_start",
      "ed\"}\n\n",
    ];

    const result: any[] = [];
    for await (const ev of parseSSE(chunks.join(""))) {
      result.push(ev);
    }

    expect(result).toHaveLength(1);
    expect(result[0].event).toBe("run_event");
    expect(result[0].id).toBe("1");
    expect(result[0].data).toEqual({ type: "run_started" });
  });

  it("handles event split across multiple chunks", async () => {
    const chunks = [
      "event: run_event\n",
      "id: 10\n",
      "data: {\"type\":\"delta\",\"payload\":{\"text\":\"H",
      "ello\"}}\n",
      "\n",
    ];

    const result: any[] = [];
    for await (const ev of parseSSE(chunks.join(""))) {
      result.push(ev);
    }

    expect(result).toHaveLength(1);
    expect(result[0].id).toBe("10");
    expect(result[0].data.payload).toEqual({ text: "Hello" });
  });

  it("yields events with event type 'delta' without id", async () => {
    const chunk =
      "event: delta\n" +
      "data: {\"run_id\":\"r1\",\"model_request\":1,\"attempt\":\"a1\",\"delta\":\"Hello \"}\n" +
      "\n";

    const result: any[] = [];
    for await (const ev of parseSSE(chunk)) {
      result.push(ev);
    }

    expect(result).toHaveLength(1);
    expect(result[0].event).toBe("delta");
    expect(result[0].id).toBeUndefined();
    expect(result[0].data).toEqual({
      run_id: "r1",
      model_request: 1,
      attempt: "a1",
      delta: "Hello ",
    });
  });

  it("yields status events without id", async () => {
    const chunk =
      "event: status\n" +
      "data: {\"display_status\":\"running\"}\n" +
      "\n";

    const result: any[] = [];
    for await (const ev of parseSSE(chunk)) {
      result.push(ev);
    }

    expect(result).toHaveLength(1);
    expect(result[0].event).toBe("status");
    expect(result[0].id).toBeUndefined();
    expect(result[0].data).toEqual({ display_status: "running" });
  });

  it("handles heartbeat comments only (no events)", async () => {
    const chunk = ": heartbeat\n: heartbeat\n";

    const result: any[] = [];
    for await (const ev of parseSSE(chunk)) {
      result.push(ev);
    }

    expect(result).toHaveLength(0);
  });

  it("handles multiple events split across arbitrary chunk boundaries", async () => {
    const fullText =
      "event: run_event\nid: 1\ndata: {\"seq\":1}\n\n" +
      "event: delta\ndata: {\"seq\":2}\n\n" +
      "event: run_event\nid: 3\ndata: {\"seq\":3}\n\n";

    const chunks: string[] = [];
    for (let i = 0; i < fullText.length; i += 7) {
      chunks.push(fullText.slice(i, i + 7));
    }

    const result: any[] = [];
    for await (const ev of parseSSE(chunks.join(""))) {
      result.push(ev);
    }

    expect(result).toHaveLength(3);
    expect(result[0].id).toBe("1");
    expect(result[1].event).toBe("delta");
    expect(result[2].id).toBe("3");
  });
  it("decodes a byte stream incrementally, including a multi-byte character split across chunks", async () => {
    const bytes = new TextEncoder().encode('event: delta\ndata: {"delta":"é🎉"}\n\n');
    const cut = bytes.indexOf(0xf0) + 1;
    const stream = new ReadableStream<Uint8Array>({
      start(c) {
        c.enqueue(bytes.slice(0, cut));
        c.enqueue(bytes.slice(cut));
        c.close();
      },
    });

    const result: any[] = [];
    for await (const ev of parseSSE(stream)) {
      result.push(ev);
    }

    expect(result).toEqual([{ event: "delta", data: { delta: "é🎉" } }]);
  });

  it("drops an incomplete event at end of stream", async () => {
    const stream = new ReadableStream<string>({
      start(c) {
        c.enqueue('id: 1\nevent: run_event\ndata: {"seq":1}\n\n');
        c.enqueue('id: 2\nevent: run_event\ndata: {"seq":');
        c.close();
      },
    });

    const result: any[] = [];
    for await (const ev of parseSSE(stream)) {
      result.push(ev);
    }

    expect(result.map((e) => e.id)).toEqual(["1"]);
  });
});
