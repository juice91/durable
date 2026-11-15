/**
 * An incremental SSE frame parser that keeps the `id:` field. `@ag-ui/client`'s `parseSSEStream`
 * drops it; `ResumableHttpAgent` needs it to resume (plan D7: every frame derived from a durable
 * event carries `id: {runId}:{seq}`).
 *
 * Follows the WHATWG event-stream rules that matter here: lines end in LF, CRLF or CR; a blank
 * line dispatches the frame; `data:` lines join with "\n"; one space after the colon is dropped;
 * lines starting with ":" (keep-alives) are ignored; an id containing NUL is ignored. A frame is
 * only returned once its blank line has arrived, and a partial frame at end of stream is discarded
 * (spec behaviour), so an id is never reported for a frame that was not fully read.
 *
 * Linear: each pushed chunk is scanned once, and a line split across chunks is kept as a list of
 * parts joined once when it ends. One frame (its pending line plus its data) may not exceed
 * `maxFrameChars` (default 10 MB, like `@ag-ui/client`'s `MAX_BUFFER_SIZE`); past it `push` throws
 * `SseFrameTooLargeError`.
 */

export const MAX_FRAME_CHARS = 10 * 1024 * 1024;

export interface SseFrame {
  /** The joined `data:` lines; absent when the frame had no data line. */
  data?: string;
  /** This frame's own `id:` field, if it carried one. */
  id?: string;
  /** The `event:` field, if any (AG-UI consumers ignore it). */
  event?: string;
}

export class SseFrameTooLargeError extends Error {
  constructor(readonly limit: number) {
    super(
      limit >= 1024 * 1024
        ? `SSE frame exceeds the ${Math.round(limit / (1024 * 1024))} MB limit`
        : `SSE frame exceeds the ${limit}-character limit`,
    );
    this.name = "SseFrameTooLargeError";
  }
}

export class SseFrameParser {
  private readonly decoder = new TextDecoder("utf-8");
  /** Parts of the current, unfinished line. */
  private lineParts: string[] = [];
  private lineChars = 0;
  /** A CR ended the previous chunk: a LF starting the next one belongs to it. */
  private pendingCr = false;
  private dataLines: string[] = [];
  private dataChars = 0;
  private id: string | undefined;
  private event: string | undefined;
  private sawField = false;

  constructor(private readonly maxFrameChars: number = MAX_FRAME_CHARS) {}

  /** Feeds bytes (or text) and returns every frame completed by them. */
  push(chunk: Uint8Array | string): SseFrame[] {
    const text = typeof chunk === "string" ? chunk : this.decoder.decode(chunk, { stream: true });
    const frames: SseFrame[] = [];
    if (text.length === 0) return frames;
    let start = 0;
    if (this.pendingCr) {
      this.pendingCr = false;
      if (text.charCodeAt(0) === 10) start = 1;
    }
    for (let index = start; index < text.length; index += 1) {
      const code = text.charCodeAt(index);
      if (code !== 10 && code !== 13) continue;
      const line = this.takeLine(text.slice(start, index));
      if (code === 13) {
        if (index + 1 === text.length) this.pendingCr = true;
        else if (text.charCodeAt(index + 1) === 10) index += 1;
      }
      start = index + 1;
      const frame = this.line(line);
      if (frame) frames.push(frame);
    }
    if (start < text.length) {
      const rest = text.slice(start);
      this.lineParts.push(rest);
      this.lineChars += rest.length;
      this.checkSize();
    }
    return frames;
  }

  /** End of stream: a frame without its terminating blank line is dropped. */
  end(): SseFrame[] {
    this.decoder.decode();
    this.lineParts = [];
    this.lineChars = 0;
    this.pendingCr = false;
    this.reset();
    return [];
  }

  private takeLine(tail: string): string {
    if (this.lineParts.length === 0) return tail;
    this.lineParts.push(tail);
    const line = this.lineParts.join("");
    this.lineParts = [];
    this.lineChars = 0;
    return line;
  }

  private checkSize(): void {
    if (this.lineChars + this.dataChars > this.maxFrameChars) {
      throw new SseFrameTooLargeError(this.maxFrameChars);
    }
  }

  private line(line: string): SseFrame | null {
    if (line === "") {
      if (!this.sawField) return null;
      const frame: SseFrame = {};
      if (this.dataLines.length > 0) frame.data = this.dataLines.join("\n");
      if (this.id !== undefined) frame.id = this.id;
      if (this.event !== undefined) frame.event = this.event;
      this.reset();
      return frame;
    }
    if (line.charCodeAt(0) === 58 /* ":" */) return null;
    const colon = line.indexOf(":");
    const field = colon === -1 ? line : line.slice(0, colon);
    let value = colon === -1 ? "" : line.slice(colon + 1);
    if (value.charCodeAt(0) === 32) value = value.slice(1);
    switch (field) {
      case "data":
        this.dataLines.push(value);
        this.dataChars += value.length + 1;
        this.sawField = true;
        this.checkSize();
        break;
      case "id":
        if (!value.includes("\0")) {
          this.id = value;
          this.sawField = true;
        }
        break;
      case "event":
        this.event = value;
        this.sawField = true;
        break;
      default:
        // `retry:` and unknown fields are ignored.
        break;
    }
    return null;
  }

  private reset(): void {
    this.dataLines = [];
    this.dataChars = 0;
    this.id = undefined;
    this.event = undefined;
    this.sawField = false;
  }
}
