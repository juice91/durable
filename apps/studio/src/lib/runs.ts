// Typed client for runs API with SSE streaming support
import { parseSSE } from "./sse";

export const BASE_URL = process.env.NEXT_PUBLIC_EAP_API_URL || "http://localhost:8000";

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
    this.name = "ApiError";
  }
}

function apiHeaders(extra: HeadersInit = {}): HeadersInit {
  const token = process.env.NEXT_PUBLIC_EAP_DEV_TOKEN;
  if (!token) throw new ApiError(0, "No API credentials configured");
  return { Authorization: token, ...extra };
}

async function apiErrorFromResponse(response: Response): Promise<ApiError> {
  let message = `HTTP ${response.status}`;
  try {
    const data = await response.json();
    if (typeof data.detail === "string") {
      message = data.detail;
    } else if (Array.isArray(data.detail)) {
      message = data.detail.map((e: { msg?: unknown }) => String(e.msg ?? "")).join(", ");
    }
  } catch {
    // Keep the status fallback for a non-JSON response.
  }
  return new ApiError(response.status, message);
}

/** `waiting_approval` (AC.6) is a display status only: a running run with an open tool approval. */
export type RunStatus = "queued" | "running" | "recovering" | "waiting_approval" | "completed" | "failed" | "needs_attention";

/**
 * What a run executed. `kb_ingest` is a knowledge-base ingestion run (the RAG branch's
 * `eap.kb_ingest` workflow): shown as "Knowledge ingest", linking to its knowledge base (RM.16).
 */
export type RunKind = "agent" | "tool" | "workflow" | "kb_ingest" | "eval";

/** Run kinds for labels and the Traces kind filter, in filter order. */
export const RUN_KIND_LABELS: Record<RunKind, string> = {
  agent: "Agent",
  workflow: "Workflow",
  tool: "Tool",
  kb_ingest: "Knowledge ingest",
  // AC.17: an agent's test suite run (its case runs are child runs).
  eval: "Eval",
};

export interface Run {
  run_id: string;
  /** "tool" for a tool test run (POST /tools/{id}/runs): agent_id and version are null, tool_id is set. */
  kind?: RunKind;
  /** Workflow runs: the workflow and its version. A workflow's agent step runs as an agent run
   * with parent_run_id / parent_step_id set. */
  workflow_id?: string | null;
  workflow_version?: number | null;
  parent_run_id?: string | null;
  parent_step_id?: string | null;
  agent_id: string | null;
  version: number | null;
  tool_id?: string | null;
  /** The knowledge base of a kb_ingest run, or of a search tester tool run (RM.16). */
  knowledge_base_id?: string | null;
  status: string;
  display_status: RunStatus;
  input: string;
  output: string | null;
  error: string | null;
  heartbeat_at: string | null;
  created_at: string;
  updated_at: string;
  trace_id: string | null;
  conversation_id: string | null;
  turn: number | null;
}

export interface RunEventData {
  /** Text for an agent run; the tool's JSON arguments (an object) for a tool run. */
  input?: string | Record<string, unknown>;
  /** Tool runs: the tool id (run_started, tool_call, tool_result) and the result (run_completed). */
  tool_id?: string;
  result?: unknown;
  version?: number;
  agent_id?: string;
  text?: string;
  message?: unknown;
  model_request?: number;
  tool_name?: string;
  tool_call_id?: string;
  args?: Record<string, unknown>;
  status?: string;
  content?: unknown;
  output?: string;
  error?: string;
  [key: string]: unknown;
}

export interface RunEvent {
  seq: number;
  type:
    | "run_started" | "model_message" | "tool_call" | "tool_result" | "run_completed" | "run_failed"
    // Workflow runs (task 3.8): one key per step and phase.
    | "step_started" | "step_waiting" | "step_approved" | "step_rejected" | "step_timed_out"
    | "step_completed" | "step_failed"
    // Processors (P0.17): one per processor that ran, keyed `processor:{phase}:{i}` (or
    // `processor:output:{i}:model:{n}`). Data: phase, index, processor_type, action, match_counts,
    // reason, categories; never the processed or matched text.
    | "processor_result"
    // RAG and memory (RM.10 to RM.14; rows in RM.19): counts, ids, model and cost, never text.
    | "embedding" | "ingest_progress" | "memory_updated" | "memory_observe_failed"
    // P1.C: the reflector's input was capped (counts only), and the end of a completed turn's
    // post-turn memory steps ({observe, index} status words).
    | "memory_observe_trimmed" | "post_turn_done"
    // Agent capabilities (AC.4, AC.6): a rejected structured-output candidate, and tool approvals.
    | "output_retry"
    | "tool_approval_waiting" | "tool_approved" | "tool_denied" | "tool_approval_timed_out"
    // Eval runs (AC.11): one per case, status and counts only.
    | "eval_case_result"
    // Workflow loops (loops D4, D12): one per iteration (`loop:{S}:started|completed|failed`), and
    // the durable cancel request (key `cancel`, {requested_by}).
    | "loop_iteration_started" | "loop_iteration_completed" | "loop_iteration_failed"
    | "cancel_requested"
    // Reusable scorers (W2.3): written by the worker after a run, key `score:{scorer_id}`.
    | "score"
    // Message feedback (W2.4): legacy, no longer written (feedback is per user; see docs/ag-ui.md).
    | "message_feedback";
  data: RunEventData;
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
      ...apiHeaders(),
    },
    body: JSON.stringify({ run_id: runId, input, version }),
  });

  if (!response.ok) {
    throw await apiErrorFromResponse(response);
  }

  return response.json();
}

export async function listRuns({
  limit = 50,
  before,
  agentId,
  status,
}: {
  limit?: number;
  before?: string;
  agentId?: string;
  status?: string;
} = {}): Promise<{ items: Run[]; next_before: string | null }> {
  const params = new URLSearchParams({ limit: String(limit) });
  if (before) params.append("before", before);
  if (agentId) params.append("agent_id", agentId);
  if (status) params.append("status", status);

  const url = `${BASE_URL}/api/v1/runs?${params.toString()}`;
  const response = await fetch(url, {
    headers: apiHeaders(),
  });

  if (!response.ok) {
    throw await apiErrorFromResponse(response);
  }

  return response.json();
}

export async function getRun(runId: string): Promise<Run> {
  const response = await fetch(`${BASE_URL}/api/v1/runs/${runId}`, {
    headers: apiHeaders(),
  });

  if (!response.ok) {
    throw await apiErrorFromResponse(response);
  }

  return response.json();
}

/** A run's stored events (after `afterSeq`); `signal` cancels the request (the memory poll, RM.18). */
export async function listEvents(runId: string, afterSeq?: number, signal?: AbortSignal): Promise<RunEvent[]> {
  const url = afterSeq !== undefined
    ? `${BASE_URL}/api/v1/runs/${runId}/events?after_seq=${afterSeq}`
    : `${BASE_URL}/api/v1/runs/${runId}/events`;

  const response = await fetch(url, {
    headers: apiHeaders(),
    ...(signal ? { signal } : {}),
  });

  if (!response.ok) {
    throw await apiErrorFromResponse(response);
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
  const authHeaders = apiHeaders({ Accept: "text/event-stream" }) as Record<string, string>;
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
      const headers: Record<string, string> = { ...authHeaders };
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
    const { model_request } = action.payload.data;
    if (model_request === undefined) return state || {};
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
