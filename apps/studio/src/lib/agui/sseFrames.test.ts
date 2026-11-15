import { describe, expect, it } from "vitest";

import { MAX_FRAME_CHARS, SseFrameParser, SseFrameTooLargeError, type SseFrame } from "./sseFrames";

const encoder = new TextEncoder();

function parseInChunks(text: string, size: number): SseFrame[] {
  const bytes = encoder.encode(text);
  const parser = new SseFrameParser();
  const frames: SseFrame[] = [];
  for (let index = 0; index < bytes.length; index += size) {
    frames.push(...parser.push(bytes.slice(index, index + size)));
  }
  frames.push(...parser.end());
  return frames;
}

const STREAM =
  'data: {"type":"RUN_STARTED"}\n\n' +
  ": keep-alive\n\n" +
  'data: {"type":"TEXT_MESSAGE_CONTENT","delta":"héllo ✓"}\n\n' +
  'id: run-1:0\ndata: {"type":"TEXT_MESSAGE_END"}\n\n' +
  'event: ignored\nretry: 100\nid: run-1:1\ndata: {"type":"RUN_FINISHED"}\n\n';

const EXPECTED: SseFrame[] = [
  { data: '{"type":"RUN_STARTED"}' },
  { data: '{"type":"TEXT_MESSAGE_CONTENT","delta":"héllo ✓"}' },
  { data: '{"type":"TEXT_MESSAGE_END"}', id: "run-1:0" },
  { data: '{"type":"RUN_FINISHED"}', id: "run-1:1", event: "ignored" },
];

describe("SseFrameParser", () => {
  it("parses ids and data whatever the chunk boundaries (including inside UTF-8 characters)", () => {
    for (let size = 1; size <= 17; size += 1) {
      expect(parseInChunks(STREAM, size)).toEqual(EXPECTED);
    }
  });

  it("accepts CRLF and CR line endings, also split between chunks", () => {
    for (const eol of ["\r\n", "\r"]) {
      const text = STREAM.replace(/\n/g, eol);
      for (const size of [1, 2, 5]) {
        expect(parseInChunks(text, size)).toEqual(EXPECTED);
      }
    }
  });

  it("drops a frame whose blank line never arrived", () => {
    const frames = parseInChunks('id: run-1:0\ndata: {"type":"A"}\n\nid: run-1:1\ndata: {"type":"B"}\n', 4);
    expect(frames).toEqual([{ data: '{"type":"A"}', id: "run-1:0" }]);
  });

  it("joins multi-line data and drops one leading space only", () => {
    const parser = new SseFrameParser();
    expect(parser.push("data:a\ndata:  b\n\n")).toEqual([{ data: "a\n b" }]);
  });

  it("ignores an id containing NUL and frames with no fields", () => {
    const parser = new SseFrameParser();
    expect(parser.push("id: a\0b\ndata: x\n\n\n\n")).toEqual([{ data: "x" }]);
  });

  it("keeps a CR / LF pair together across an empty chunk", () => {
    const parser = new SseFrameParser();
    const frames = [
      ...parser.push(encoder.encode("data: a\r")),
      ...parser.push(new Uint8Array(0)),
      ...parser.push(""),
      ...parser.push(encoder.encode("\ndata: b\r\n\r\n")),
    ];
    expect(frames).toEqual([{ data: "a\nb" }]);
  });

  it("parses a 4 MiB line fed in 16 KiB chunks in linear time", () => {
    const payload = "x".repeat(4 * 1024 * 1024);
    const bytes = encoder.encode(`id: r:1\ndata: ${payload}\n\n`);
    const parser = new SseFrameParser();
    const started = performance.now();
    const frames: SseFrame[] = [];
    for (let index = 0; index < bytes.length; index += 16 * 1024) {
      frames.push(...parser.push(bytes.subarray(index, index + 16 * 1024)));
    }
    const elapsed = performance.now() - started;
    expect(frames).toHaveLength(1);
    expect(frames[0].data).toHaveLength(payload.length);
    expect(frames[0].id).toBe("r:1");
    // the quadratic version took ~1.3 s here
    expect(elapsed).toBeLessThan(300);
  });

  it("throws a clear error once one frame passes the cap (one long line or many data lines)", () => {
    const line = new SseFrameParser(1000);
    line.push("data: " + "x".repeat(600));
    expect(() => line.push("x".repeat(600))).toThrow(SseFrameTooLargeError);
    const many = new SseFrameParser(1000);
    for (let index = 0; index < 9; index += 1) many.push("data: " + "y".repeat(100) + "\n");
    expect(() => many.push("data: " + "y".repeat(100) + "\n")).toThrow("SSE frame exceeds the 1000-character limit");
    // the default cap is 10 MB, and a frame under it passes
    expect(MAX_FRAME_CHARS).toBe(10 * 1024 * 1024);
    const ok = new SseFrameParser(1000);
    expect(ok.push("data: " + "z".repeat(900) + "\n\n")).toHaveLength(1);
    expect(ok.push("data: " + "z".repeat(900) + "\n\n")).toHaveLength(1);
  });
});

