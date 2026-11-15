import {
  EventType,
  HttpAgent,
  enforceOutgoingInput,
  type BaseEvent,
  type HttpAgentConfig,
  type RunAgentInput,
} from "@ag-ui/client";
import { Observable } from "rxjs";

import { SseFrameParser, SseFrameTooLargeError } from "./sseFrames";

/**
 * `HttpAgent` with resume (plan D7, "Resume protocol"). Used by the agent Chat tab, the AG-UI smoke
 * test, and documented for external clients.
 *
 * 1. The response body is parsed here, keeping the SSE `id:` field that `HttpAgent` drops.
 *    `lastEventId` advances only after a frame carrying an id has been fully read and its event
 *    emitted, and is kept per `(threadId, runId)` in sessionStorage (failures tolerated).
 * 2. On a network error, a 5xx, or a stream that ends without `RUN_FINISHED` / `RUN_ERROR`, the agent
 *    emits local `TEXT_MESSAGE_END` / `TOOL_CALL_END` for anything open, then re-POSTs the identical
 *    body with `Last-Event-ID: lastEventId` (`{runId}:-1` when only `RUN_STARTED` arrived; no header
 *    when nothing arrived) and splices the continuation into the same run.
 * 3. Up to 5 retries, backoff 0.5 s → 8 s, ×2, ±20% jitter; the budget resets after progress (a new
 *    frame id, or an event that is not the continuation's `CUSTOM pydanticos.run` /
 *    `MESSAGES_SNAPSHOT` preamble), and a run reconnects at most `maxReconnects` (30) times in all.
 *    408 and 429 are retried, honouring `Retry-After`. Any other 4xx stops and emits a local
 *    `RUN_ERROR{code:"stream.resume_failed"}`. A server `RUN_ERROR{code:"stream_interrupted"}` (the
 *    API failed, the run continues) is not passed on: it is a drop and is resumed. `abortRun()`
 *    never retries (the server run continues).
 * 4. `connect()` is the GET attach route with the stored `lastEventId`, so `connectAgent()` and
 *    assistant-ui's `unstable_resume` reattach after a reload; `detachActiveRun()` closes the fetch.
 *    The run is `attachRunId`, else the thread's stored active run, else `input.runId` if this tab
 *    stored an id for it. With no such run, or a 404 from the attach
 *    route before any event, it completes with no events. A continuation opens the run and any step
 *    it finishes locally (`RUN_STARTED`, `STEP_STARTED`), since the client holds no state from the
 *    frames before `Last-Event-ID`.
 */

export const RESUME_FAILED_CODE = "stream.resume_failed";
export const MAX_RESUME_ATTEMPTS = 5;
export const BACKOFF_BASE_MS = 500;
export const BACKOFF_MAX_MS = 8000;
export const BACKOFF_JITTER = 0.2;
/** Reconnects per run in all, progress or not. */
export const MAX_RECONNECTS = 30;
/** The longest `Retry-After` honoured. */
export const MAX_RETRY_AFTER_MS = 60_000;
/** The server's code for "the API failed after the stream opened; the run continues; resume" (D9). */
export const STREAM_INTERRUPTED_CODE = "stream_interrupted";

/** sessionStorage key prefixes (this tab only). */
export const LAST_EVENT_ID_KEY_PREFIX = "eap.studio.agui.lastEventId:";
export const ACTIVE_RUN_KEY_PREFIX = "eap.studio.agui.activeRun:";

export function lastEventIdKey(threadId: string, runId: string): string {
  return `${LAST_EVENT_ID_KEY_PREFIX}${threadId}:${runId}`;
}

export function activeRunKey(threadId: string): string {
  return `${ACTIVE_RUN_KEY_PREFIX}${threadId}`;
}

/**
 * The delay before retry `attempt` (1-based): 0.5 s, 1 s, 2 s, 4 s, 8 s (capped), each ±20%.
 * `random` returns [0, 1).
 */
export function backoffDelayMs(attempt: number, random: () => number = Math.random): number {
  const base = Math.min(BACKOFF_BASE_MS * 2 ** Math.max(0, attempt - 1), BACKOFF_MAX_MS);
  const factor = 1 + BACKOFF_JITTER * (2 * random() - 1);
  return Math.round(base * factor);
}

/**
 * A `Retry-After` header in ms (delta-seconds or an HTTP date), capped at `MAX_RETRY_AFTER_MS`;
 * null when absent or unreadable.
 */
export function retryAfterMs(header: string | null, now: number = Date.now()): number | null {
  if (!header) return null;
  const trimmed = header.trim();
  let ms: number;
  if (/^\d+$/.test(trimmed)) ms = Number(trimmed) * 1000;
  else {
    const date = Date.parse(trimmed);
    if (Number.isNaN(date)) return null;
    ms = date - now;
  }
  return Math.min(Math.max(0, ms), MAX_RETRY_AFTER_MS);
}

/** The `GET .../threads/{threadId}/runs/{runId}/stream` attach route under an agent or workflow URL. */
export function defaultAttachUrl(url: string, threadId: string, runId: string): string {
  return `${url.replace(/\/+$/, "")}/threads/${encodeURIComponent(threadId)}/runs/${encodeURIComponent(runId)}/stream`;
}

type StorageSource = Storage | null | (() => Storage | null);

export interface ResumableHttpAgentConfig extends HttpAgentConfig {
  /** Builds the GET attach URL; default `defaultAttachUrl(url, …)`. */
  attachUrl?: (threadId: string, runId: string) => string;
  /** Where `lastEventId` is kept; default `window.sessionStorage` (null: memory only). */
  storage?: StorageSource;
  /** Waits between retries; must reject or resolve early when `signal` aborts. */
  sleep?: (ms: number, signal: AbortSignal) => Promise<void>;
  /** Jitter source, [0, 1). */
  random?: () => number;
  /** Retries after a drop without progress (default 5). */
  maxAttempts?: number;
  /** Reconnects per run in all (default 30). */
  maxReconnects?: number;
}

/** An HTTP error before any event of the run was emitted (same shape as `HttpAgent`'s). */
export class AgUiHttpError extends Error {
  constructor(
    readonly status: number,
    readonly payload: unknown,
  ) {
    super(`HTTP ${status}: ${typeof payload === "string" ? payload : JSON.stringify(payload)}`);
    this.name = "AgUiHttpError";
  }
}

function defaultStorage(): Storage | null {
  try {
    return typeof window === "undefined" ? null : window.sessionStorage;
  } catch {
    return null;
  }
}

function defaultSleep(ms: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve) => {
    if (signal.aborted) {
      resolve();
      return;
    }
    const timer = setTimeout(done, ms);
    function done() {
      clearTimeout(timer);
      signal.removeEventListener("abort", done);
      resolve();
    }
    signal.addEventListener("abort", done, { once: true });
  });
}

/** A string id field of an event (`messageId`, `toolCallId`). */
function idOf(event: BaseEvent, field: "messageId" | "toolCallId"): string {
  return String((event as unknown as Record<string, unknown>)[field]);
}

function isAbortError(error: unknown): boolean {
  return (error as { name?: unknown } | null)?.name === "AbortError";
}

async function readErrorPayload(response: Response): Promise<unknown> {
  let text = "";
  try {
    text = await response.text();
  } catch {
    return "";
  }
  if ((response.headers.get("content-type") ?? "").includes("application/json")) {
    try {
      return JSON.parse(text);
    } catch {
      return text;
    }
  }
  return text;
}

type Mode = "post" | "attach";

export class ResumableHttpAgent extends HttpAgent {
  /** The run `connect()` attaches to; when unset, the thread's stored active run, else the input's. */
  attachRunId?: string;
  // Not readonly: clone() (AbstractAgent builds the copy without running constructors) sets them.
  private attachUrlOption: ((threadId: string, runId: string) => string) | undefined;
  private storageSource: StorageSource | undefined;
  private sleepFn: (ms: number, signal: AbortSignal) => Promise<void>;
  private randomFn: () => number;
  private maxAttempts: number;
  private maxReconnects: number;
  private memoryIds: Map<string, string>;

  constructor(config: ResumableHttpAgentConfig) {
    super(config);
    this.attachUrlOption = config.attachUrl;
    this.storageSource = config.storage;
    this.sleepFn = config.sleep ?? defaultSleep;
    this.randomFn = config.random ?? Math.random;
    this.maxAttempts = config.maxAttempts ?? MAX_RESUME_ATTEMPTS;
    this.maxReconnects = config.maxReconnects ?? MAX_RECONNECTS;
    this.memoryIds = new Map();
  }

  /** The GET attach URL for a run. */
  attachUrl(threadId: string, runId: string): string {
    return this.attachUrlOption ? this.attachUrlOption(threadId, runId) : defaultAttachUrl(this.url, threadId, runId);
  }

  /** The last fully read frame id for `(threadId, runId)`, or null. */
  getLastEventId(threadId: string, runId: string): string | null {
    const key = lastEventIdKey(threadId, runId);
    try {
      const stored = this.storage()?.getItem(key);
      if (stored) return stored;
    } catch {
      // fall back to memory
    }
    return this.memoryIds.get(key) ?? null;
  }

  /** The run this tab last streamed on `threadId` and has not seen finish, or null. */
  getActiveRunId(threadId: string): string | null {
    const key = activeRunKey(threadId);
    try {
      const stored = this.storage()?.getItem(key);
      if (stored) return stored;
    } catch {
      // fall back to memory
    }
    return this.memoryIds.get(key) ?? null;
  }

  run(input: RunAgentInput): Observable<BaseEvent> {
    return this.stream(input, "post");
  }

  protected connect(input: RunAgentInput): Observable<BaseEvent> {
    // AbstractAgent fills input.runId with a fresh uuid when none is given, so input.runId is used
    // only when this tab stored an id for it (an explicit connectAgent({runId}) of a known run).
    const runId =
      this.attachRunId ??
      this.getActiveRunId(input.threadId) ??
      (this.getLastEventId(input.threadId, input.runId) ? input.runId : null);
    if (!runId) return new Observable<BaseEvent>((subscriber) => subscriber.complete());
    // connectAgent() does not reset the controller the way runAgent() does.
    if (this.abortController.signal.aborted) this.abortController = new AbortController();
    return this.stream({ ...input, runId }, "attach");
  }

  clone(): ResumableHttpAgent {
    // super.clone() copies url, headers, fetch and a fresh abortController; our fields by hand.
    const cloned = super.clone() as ResumableHttpAgent;
    cloned.attachRunId = this.attachRunId;
    cloned.attachUrlOption = this.attachUrlOption;
    cloned.storageSource = this.storageSource;
    cloned.sleepFn = this.sleepFn;
    cloned.randomFn = this.randomFn;
    cloned.maxAttempts = this.maxAttempts;
    cloned.maxReconnects = this.maxReconnects;
    cloned.memoryIds = new Map(this.memoryIds);
    return cloned;
  }

  private storage(): Storage | null {
    const source = this.storageSource;
    if (source === undefined) return defaultStorage();
    if (typeof source === "function") {
      try {
        return source();
      } catch {
        return null;
      }
    }
    return source;
  }

  private remember(key: string, value: string | null): void {
    if (value === null) this.memoryIds.delete(key);
    else this.memoryIds.set(key, value);
    try {
      const storage = this.storage();
      if (!storage) return;
      if (value === null) storage.removeItem(key);
      else storage.setItem(key, value);
    } catch {
      // storage blocked or full: memory keeps the value for this agent
    }
  }

  private stream(input: RunAgentInput, mode: Mode): Observable<BaseEvent> {
    return new Observable<BaseEvent>((subscriber) => {
      const userSignal = this.abortController.signal;
      const local = new AbortController();
      const onUserAbort = () => local.abort();
      if (userSignal.aborted) local.abort();
      else userSignal.addEventListener("abort", onUserAbort, { once: true });

      const { threadId, runId } = input;
      const idKey = lastEventIdKey(threadId, runId);
      const runKey = activeRunKey(threadId);
      const openMessages = new Set<string>();
      const openToolCalls = new Set<string>();
      /** Steps this Observable has seen start and not finish (they survive a resume). */
      const openSteps = new Set<string>();
      let lastEventId: string | null = mode === "attach" ? this.getLastEventId(threadId, runId) : null;
      /** An attach from a stored id: the client never saw the frames before it. */
      const continuation = lastEventId !== null;
      let sawRunStarted = false;
      let emittedAny = false;
      let terminal = false;
      let closed = false;

      const emit = (event: BaseEvent) => {
        if (closed) return;
        if (!sawRunStarted && event.type !== EventType.RUN_STARTED && event.type !== EventType.RUN_ERROR) {
          // A continuation (attach with a stored id) has no RUN_STARTED, but a run must open with one.
          emit({ type: EventType.RUN_STARTED, threadId, runId } as BaseEvent);
        }
        if (
          continuation &&
          event.type === EventType.STEP_FINISHED &&
          !openSteps.has(stepName(event))
        ) {
          // The step started before Last-Event-ID; open it locally so the verifier accepts its end.
          emit({ type: EventType.STEP_STARTED, stepName: stepName(event) } as BaseEvent);
        }
        emittedAny = true;
        switch (event.type) {
          case EventType.RUN_STARTED:
            sawRunStarted = true;
            break;
          case EventType.TEXT_MESSAGE_START:
            openMessages.add(idOf(event, "messageId"));
            break;
          case EventType.TEXT_MESSAGE_END:
            openMessages.delete(idOf(event, "messageId"));
            break;
          case EventType.TOOL_CALL_START:
            openToolCalls.add(idOf(event, "toolCallId"));
            break;
          case EventType.TOOL_CALL_END:
            openToolCalls.delete(idOf(event, "toolCallId"));
            break;
          case EventType.STEP_STARTED:
            openSteps.add(stepName(event));
            break;
          case EventType.STEP_FINISHED:
            openSteps.delete(stepName(event));
            break;
          case EventType.RUN_FINISHED:
          case EventType.RUN_ERROR:
            terminal = true;
            break;
          default:
            break;
        }
        subscriber.next(event);
      };

      const closeOpenItems = () => {
        for (const messageId of openMessages) {
          emit({ type: EventType.TEXT_MESSAGE_END, messageId } as BaseEvent);
        }
        for (const toolCallId of openToolCalls) {
          emit({ type: EventType.TOOL_CALL_END, toolCallId } as BaseEvent);
        }
        openMessages.clear();
        openToolCalls.clear();
      };

      const finishAborted = () => {
        // Same as HttpAgent: a user abort is a local RUN_ERROR; the server run continues.
        emit({ type: EventType.RUN_ERROR, message: "Request aborted", code: "abort" } as BaseEvent);
        subscriber.complete();
      };

      const failResume = (message: string, clearStored: boolean) => {
        closeOpenItems();
        if (clearStored) {
          this.remember(idKey, null);
          this.remember(runKey, null);
        }
        emit({ type: EventType.RUN_ERROR, message, code: RESUME_FAILED_CODE } as BaseEvent);
        subscriber.complete();
      };

      const buildRequest = (): { url: string; init: RequestInit } => {
        if (mode === "attach") {
          return {
            url: this.attachUrl(threadId, runId),
            init: { method: "GET", headers: { ...this.headers, Accept: "text/event-stream" } },
          };
        }
        const init = this.requestInit(enforceOutgoingInput(input));
        return { url: this.url, init };
      };

      const go = async () => {
        let request: { url: string; init: RequestInit };
        try {
          request = buildRequest();
        } catch (error) {
          subscriber.error(error);
          return;
        }
        if (mode === "post") this.remember(runKey, runId);

        let failures = 0;
        let reconnects = 0;
        while (!closed) {
          const headers = new Headers(request.init.headers ?? {});
          const resumeFrom = lastEventId ?? (sawRunStarted ? `${runId}:-1` : null);
          if (resumeFrom) headers.set("Last-Event-ID", resumeFrom);
          const resuming = resumeFrom !== null;
          let progressed = false;
          let retryReason = "";
          let minDelayMs = 0;
          try {
            const response = await this.fetch(request.url, {
              ...request.init,
              headers,
              signal: local.signal,
            });
            if (!response.ok) {
              const payload = await readErrorPayload(response);
              const retryable = response.status === 408 || response.status === 429;
              if (retryable) {
                minDelayMs = retryAfterMs(response.headers.get("Retry-After")) ?? 0;
              } else if (mode === "attach" && response.status === 404 && !emittedAny) {
                // The run is gone (or never existed here): nothing to attach to.
                this.remember(idKey, null);
                this.remember(runKey, null);
                subscriber.complete();
                return;
              } else if (response.status >= 400 && response.status < 500) {
                if (!emittedAny) {
                  this.remember(idKey, null);
                  this.remember(runKey, null);
                  subscriber.error(new AgUiHttpError(response.status, payload));
                  return;
                }
                failResume(`Resume failed: HTTP ${response.status}`, true);
                return;
              }
              throw new AgUiHttpError(response.status, payload);
            }
            const reader = response.body?.getReader();
            if (!reader) throw new Error("Failed to getReader() from response");
            const parser = new SseFrameParser();
            const onFrames = (frames: ReturnType<SseFrameParser["push"]>) => {
              for (const frame of frames) {
                if (frame.data !== undefined) {
                  const event = JSON.parse(frame.data) as unknown;
                  if (
                    typeof event !== "object" ||
                    event === null ||
                    Array.isArray(event) ||
                    typeof (event as { type?: unknown }).type !== "string"
                  ) {
                    throw new ProtocolError("Invalid event: the frame is not an AG-UI event.");
                  }
                  const typed = event as BaseEvent & { code?: unknown; name?: unknown };
                  if (typed.type === EventType.RUN_ERROR && typed.code === STREAM_INTERRUPTED_CODE) {
                    // Not the run's end (D9): the API side failed and the client resumes.
                    throw new StreamInterrupted();
                  }
                  emit(typed);
                  if (!(resuming && isContinuationPreamble(typed))) progressed = true;
                }
                if (frame.id && frame.id !== lastEventId) {
                  lastEventId = frame.id;
                  this.remember(idKey, frame.id);
                  progressed = true;
                }
              }
            };
            try {
              for (;;) {
                const { done, value } = await reader.read();
                if (closed) return;
                if (done) break;
                if (value) onFrames(parser.push(value));
              }
            } catch (error) {
              if (
                error instanceof ProtocolError ||
                error instanceof SyntaxError ||
                error instanceof SseFrameTooLargeError
              ) {
                void reader.cancel().catch(() => undefined);
                subscriber.error(error);
                return;
              }
              if (error instanceof StreamInterrupted) void reader.cancel().catch(() => undefined);
              throw error;
            }
            onFrames(parser.end());
            if (terminal) {
              this.remember(idKey, null);
              this.remember(runKey, null);
              subscriber.complete();
              return;
            }
            retryReason = "stream ended before the run finished";
          } catch (error) {
            if (closed) return;
            if (userSignal.aborted || (isAbortError(error) && local.signal.aborted)) {
              finishAborted();
              return;
            }
            retryReason = error instanceof Error ? error.message : String(error);
          }

          if (closed) return;
          if (terminal) {
            // The run's terminal event arrived; only the trailing bytes were lost.
            this.remember(idKey, null);
            this.remember(runKey, null);
            subscriber.complete();
            return;
          }
          closeOpenItems();
          if (progressed) failures = 0;
          failures += 1;
          reconnects += 1;
          const exhausted = failures > this.maxAttempts || reconnects > this.maxReconnects;
          if (exhausted) {
            const limit =
              failures > this.maxAttempts ? `${this.maxAttempts} retries` : `${this.maxReconnects} reconnects`;
            if (!emittedAny) {
              subscriber.error(new Error(`AG-UI stream failed after ${limit}: ${retryReason}`));
              return;
            }
            failResume(`Resume failed after ${limit}: ${retryReason}`, false);
            return;
          }
          try {
            await this.sleepFn(Math.max(backoffDelayMs(failures, this.randomFn), minDelayMs), local.signal);
          } catch {
            // an aborted sleep is handled below
          }
          if (closed) return;
          if (userSignal.aborted) {
            finishAborted();
            return;
          }
        }
      };

      void go().catch((error: unknown) => {
        if (!closed) subscriber.error(error);
      });

      return () => {
        closed = true;
        userSignal.removeEventListener("abort", onUserAbort);
        local.abort();
      };
    });
  }
}

/** The continuation's preamble (D7): replayed state, not progress. */
function isContinuationPreamble(event: BaseEvent & { name?: unknown }): boolean {
  return (
    event.type === EventType.MESSAGES_SNAPSHOT ||
    event.type === EventType.STATE_SNAPSHOT ||
    (event.type === EventType.CUSTOM && event.name === "pydanticos.run")
  );
}

function stepName(event: BaseEvent): string {
  return String((event as unknown as { stepName?: unknown }).stepName);
}

class StreamInterrupted extends Error {
  constructor() {
    super(`server reported ${STREAM_INTERRUPTED_CODE}`);
    this.name = "StreamInterrupted";
  }
}

class ProtocolError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "ProtocolError";
  }
}
