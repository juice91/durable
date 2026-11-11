// Typed client for runs API with SSE streaming support
// Reuses auth/base-URL logic by duplicating minimal constants (api.ts doesn't export helpers)

import { parseSSE } from "./sse";

export const BASE_URL = process.env.NEXT_PUBLIC_EAP_API_URL || "http://localhost:8000";
export const TOKEN = process.env.NEXT_PUBLIC_EAP_DEV_TOKEN || "Bearer dev:acme:alice";

export type RunStatus = "queued" | "running" | "recovering" | "completed" | "failed" | "needs_attention";

export interface Run {
  id: string;
  status: string;
  display_status: RunStatus;
  agent_id: string;
  version: number | null;
  created_at?: string;
  updated_at?: string;
  heartbeat_at?: string;
  error?: string;
}

export interface RunEvent {
  seq: number;
  type: "run_started" | "model_message" | "tool_call" | "tool_result" | "run_completed" | "run_failed";
  data: unknown;
  ts: string;
}

export interface RunDelta {
  run_id: string;
  model_request: number;
  attempt: string;
  delta: string;
}

export interface RunStatusFrame {
  run_id?: string;
  status?: string;
  display_status: RunStatus;
}

export type EventHandler = (event: RunEvent) => void;
export type StatusHandler = (status: RunStatusFrame) => void;
export type DeltaHandler = (delta: RunDelta) => void;

/** Why a subscription stopped for good (unsubscribe calls no handler). */
export type StreamEndReason = "run_completed" | "run_failed" | "terminal_status" | "not_found";

export interface SubscribeHandlers {
  onEvent?: EventHandler;
  onStatus?: StatusHandler;
  onDelta?: DeltaHandler;
  onEnd?: (reason: StreamEndReason) => void;
  /** A connection attempt failed or dropped; the subscription keeps retrying. */
  onError?: (error: unknown) => void;
}

export interface SubscribeOptions {
  /** Last durable seq the caller already has; -1 (default) means nothing seen yet. */
  initialLastSeq?: number;
  /** First reconnect delay; doubles per consecutive attempt without progress. */
  baseDelayMs?: number;
  maxDelayMs?: number;
}

const TERMINAL_EVENT_TYPES = new Set(["run_completed", "run_failed"]);
const TERMINAL_RUN_STATUSES = new Set(["completed", "failed", "needs_attention"]);

export async function startRun(
  agentId: string,
  runId: string,
  input: string,
  version: number | null = null
): Promise<Run> {
  const response = await fetch(`${BASE_URL}/api/v1/agents/${agentId}/runs`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      Authorization: TOKEN,
    },
    body: JSON.stringify({ run_id: runId, input, version }),
  });

  if (!response.ok) {
    const errText = await response.text();
    throw new Error(`Failed to start run: ${response.status} ${errText}`);
  }

  return response.json();
}

export async function getRun(runId: string): Promise<Run> {
  const response = await fetch(`${BASE_URL}/api/v1/runs/${runId}`, {
    headers: {
      Authorization: TOKEN,
    },
  });

  if (!response.ok) {
    const errText = await response.text();
    throw new Error(`Failed to get run: ${response.status} ${errText}`);
  }

  return response.json();
}

export async function listEvents(runId: string, afterSeq?: number): Promise<RunEvent[]> {
  const url = afterSeq !== undefined
    ? `${BASE_URL}/api/v1/runs/${runId}/events?after_seq=${afterSeq}`
    : `${BASE_URL}/api/v1/runs/${runId}/events`;

  const response = await fetch(url, {
    headers: {
      Authorization: TOKEN,
    },
  });

  if (!response.ok) {
    const errText = await response.text();
    throw new Error(`Failed to list events: ${response.status} ${errText}`);
  }

  return response.json();
}

/**
 * Follow a run's SSE stream until it ends, reconnecting with capped exponential backoff.
 *
 * - Durable `run_event` frames reach `onEvent` at most once and in order: anything with
 *   seq <= the last received seq is dropped (seq 0 is the first real event, -1 means none yet).
 * - Each attempt resumes from the last received seq with `Last-Event-ID` (omitted on a fresh start).
 * - `delta` and `status` frames are forwarded as-is; partial text is the caller's job (deltaReducer).
 * - It stops for good after `run_completed`/`run_failed`, a 404, a clean close right after a
 *   terminal run status (the server's way of saying "you already have the final event"), or when
 *   the returned function is called, which also aborts the in-flight fetch.
 * All state is per subscription.
 */
export function subscribeRun(
  runId: string,
  handlers: SubscribeHandlers,
  opts: SubscribeOptions = {}
): () => void {
  const baseDelay = opts.baseDelayMs ?? 500;
  const maxDelay = opts.maxDelayMs ?? 10_000;
  const url = `${BASE_URL}/api/v1/runs/${encodeURIComponent(runId)}/stream`;

  let lastSeq = opts.initialLastSeq ?? -1;
  let stopped = false;
  let controller: AbortController | null = null;
  let wake: (() => void) | null = null;
  let timer: ReturnType<typeof setTimeout> | null = null;

  const sleep = (ms: number) =>
    new Promise<void>((resolve) => {
      wake = resolve;
      timer = setTimeout(resolve, ms);
    }).finally(() => {
      wake = null;
      timer = null;
    });

  const end = (reason: StreamEndReason) => {
    if (stopped) return;
    stopped = true;
    handlers.onEnd?.(reason);
  };

  const loop = async () => {
    let failures = 0;
    while (!stopped) {
      const ac = new AbortController();
      controller = ac;
      const headers: Record<string, string> = { Authorization: TOKEN, Accept: "text/event-stream" };
      if (lastSeq >= 0) headers["Last-Event-ID"] = String(lastSeq);

      let progressed = false;
      let terminalStatus = false;
      try {
        const response = await fetch(url, { headers, signal: ac.signal, cache: "no-store" });
        if (stopped) return;
        if (response.status === 404) {
          end("not_found");
          return;
        }
        if (!response.ok || !response.body) {
          throw new Error(`run stream failed: HTTP ${response.status}`);
        }

        for await (const frame of parseSSE(response.body)) {
          if (stopped) return;
          if (frame.event === "run_event") {
            const event = frame.data as RunEvent;
            const seq = frame.id !== undefined ? Number(frame.id) : event?.seq;
            if (typeof seq !== "number" || !Number.isInteger(seq) || seq <= lastSeq) continue;
            lastSeq = seq;
            progressed = true;
            handlers.onEvent?.(event);
            if (TERMINAL_EVENT_TYPES.has(event.type)) {
              end(event.type as StreamEndReason);
              return;
            }
          } else if (frame.event === "delta") {
            handlers.onDelta?.(frame.data as RunDelta);
          } else if (frame.event === "status") {
            const status = frame.data as RunStatusFrame;
            terminalStatus = TERMINAL_RUN_STATUSES.has(status?.status ?? "");
            handlers.onStatus?.(status);
          }
        }
        // Clean close without a terminal event.
        if (terminalStatus && !progressed) {
          end("terminal_status");
          return;
        }
      } catch (error) {
        if (stopped) return;
        handlers.onError?.(error);
      } finally {
        if (controller === ac) controller = null;
      }

      if (stopped) return;
      failures = progressed ? 0 : failures + 1;
      await sleep(Math.min(baseDelay * 2 ** Math.max(failures - 1, 0), maxDelay));
    }
  };

  void loop();

  return () => {
    stopped = true;
    controller?.abort();
    controller = null;
    if (timer !== null) clearTimeout(timer);
    wake?.();
  };
}

// Delta reducer for managing partial text
export interface PartialTextMap {
  [modelRequest: string]: {
    text: string;
    attempt: string;
  };
}

export type DeltaAction =
  | { type: "init" }
  | { type: "delta"; payload: RunDelta }
  | { type: "model_message"; payload: RunEvent }
  | { type: "done" }
  | { type: "failed" };

export function deltaReducer(state: PartialTextMap | undefined, action: DeltaAction): PartialTextMap {
  if (action.type === "init") {
    return {};
  }

  if (action.type === "delta") {
    const { model_request, attempt, delta } = action.payload;
    const key = String(model_request);

    // Check if this is a new attempt for the same model_request
    if (state?.[key] && state[key].attempt !== attempt) {
      // New attempt - replace the entire partial text
      return {
        ...state,
        [key]: {
          text: delta,
          attempt,
        },
      };
    }

    // Same attempt - append
    return {
      ...state,
      [key]: {
        text: (state?.[key]?.text || "") + delta,
        attempt,
      },
    };
  }

  if (action.type === "model_message") {
    const { model_request } = action.payload.data as { model_request: number };
    const key = String(model_request);

    const newState = { ...state };
    delete newState[key];
    return newState;
  }

  if (action.type === "done" || action.type === "failed") {
    return {};
  }

  return state || {};
}
