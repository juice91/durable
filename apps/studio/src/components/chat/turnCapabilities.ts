// What the agent capabilities milestone adds to one chat turn (AC.14), read from its run events:
// structured output (AC.4), fallback models and usage limits (AC.5), and tool approvals (AC.6).
// Events carry structured fields only (codes, ids, paths, counts), never provider error text.
import type { AgentSpec } from "@/lib/api";
import type { RunEvent, RunStatus } from "@/lib/runs";

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/** The chat's data part names (thread.ts emits them; ChatPane maps them to capabilityParts.tsx). */
export const STRUCTURED_OUTPUT_PART = "structured_output";
export const OUTPUT_RETRY_PART = "output_retry";
export const TOOL_APPROVAL_PART = "tool_approval";

// ------------------------------------------------------------------------- structured output

/** Whether a spec returns validated JSON (`output` set): its turns stream no live text. */
export function hasStructuredOutput(spec: AgentSpec | null | undefined): boolean {
  return isRecord((spec as { output?: unknown } | null | undefined)?.output);
}

/** The validated object of a completed structured turn (`run_completed.output_json`), else undefined. */
export function turnOutputJson(events: readonly RunEvent[]): unknown {
  for (const event of events) {
    if (event.type === "run_completed" && "output_json" in event.data) return event.data.output_json;
  }
  return undefined;
}

/** Whether the turn's events show it is structured (a candidate, a retry or an object). */
export function turnLooksStructured(events: readonly RunEvent[]): boolean {
  return events.some(
    (event) =>
      (event.type === "model_message" && event.data.output_candidate === true) ||
      event.type === "output_retry" ||
      (event.type === "run_completed" && "output_json" in event.data),
  );
}

/** One rejected candidate (`output_retry`): its model request and the JSON paths that failed. */
export interface OutputRetry {
  modelRequest: number | null;
  errors: string[];
}

export function outputRetryOf(data: Record<string, unknown>): OutputRetry {
  return {
    modelRequest: typeof data.model_request === "number" ? data.model_request : null,
    errors: Array.isArray(data.errors) ? data.errors.filter((e): e is string => typeof e === "string") : [],
  };
}

/** "Retrying: the answer did not match the output schema (at priority, tags[0])." */
export function outputRetryText(retry: OutputRetry): string {
  const paths = retry.errors.map((path) => path || "(root)");
  const shown = paths.slice(0, 5).join(", ") + (paths.length > 5 ? `, +${paths.length - 5} more` : "");
  return `The answer did not match the output schema${shown ? ` (at ${shown})` : ""}; the model was asked again.`;
}

// ------------------------------------------------------------------------------ fallback

export interface FailedAttempt {
  modelId: string;
  outcome: string | null;
  errorType: string | null;
  httpStatus: number | null;
}

export interface TurnFallback {
  /** Model requests answered by a fallback model (fallback_index > 0). */
  answeredByFallback: number;
  /** Every failed attempt of the turn's requests, in order. */
  attempts: FailedAttempt[];
  /** The primary model of the chain (`requested_model_id`). */
  primary: string | null;
  /** The model that answered the turn's last fallback-answered request (`answering_model`, when
   * the worker records it; null on events from before it did). */
  answeringModel: string | null;
}

/** The turn's fallback activity, or null when no request ran on a chain that failed over. */
export function turnFallback(events: readonly RunEvent[]): TurnFallback | null {
  let answeredByFallback = 0;
  let primary: string | null = null;
  let answeringModel: string | null = null;
  const attempts: FailedAttempt[] = [];
  for (const event of events) {
    if (event.type !== "model_message") continue;
    const data = event.data;
    if (typeof data.requested_model_id === "string") primary = data.requested_model_id;
    if (typeof data.fallback_index === "number" && data.fallback_index > 0) {
      answeredByFallback += 1;
      if (typeof data.answering_model === "string" && data.answering_model) answeringModel = data.answering_model;
    }
    if (!Array.isArray(data.failed_attempts)) continue;
    for (const item of data.failed_attempts) {
      if (!isRecord(item)) continue;
      attempts.push({
        modelId: typeof item.model_id === "string" ? item.model_id : "unknown model",
        outcome: typeof item.outcome === "string" ? item.outcome : null,
        errorType: typeof item.error_type === "string" ? item.error_type : null,
        httpStatus: typeof item.http_status === "number" ? item.http_status : null,
      });
    }
  }
  if (answeredByFallback === 0 && attempts.length === 0) return null;
  return { answeredByFallback, attempts, primary, answeringModel };
}

/** "test:fail-503 (ModelHTTPError, HTTP 503)". */
export function attemptText(attempt: FailedAttempt): string {
  const why = [attempt.errorType, attempt.httpStatus !== null ? `HTTP ${attempt.httpStatus}` : null].filter(Boolean).join(", ");
  return why ? `${attempt.modelId} (${why})` : attempt.modelId;
}

/** Whether the turn's model override switched fallback off (`run_started.fallback`). */
export function fallbackDisabledByOverride(events: readonly RunEvent[]): boolean {
  return events.some((event) => event.type === "run_started" && event.data.fallback === "disabled_by_override");
}

// ------------------------------------------------------------------- limits and other failures

export const LIMIT_LABELS: Record<string, string> = {
  max_requests: "max requests",
  max_tool_calls: "max tool calls",
  max_input_tokens: "max input tokens",
  max_output_tokens: "max output tokens",
  max_total_tokens: "max total tokens",
  max_cost_usd: "max cost",
};

export type CapabilityFailureCode =
  | "usage_limit_exceeded"
  | "run_budget_exceeded"
  | "fallback_exhausted"
  | "output_invalid"
  | "output_mode_unsupported";

const FAILURE_TITLES: Record<CapabilityFailureCode, string> = {
  usage_limit_exceeded: "Usage limit exceeded",
  run_budget_exceeded: "Cost limit reached",
  fallback_exhausted: "Every model failed",
  output_invalid: "Invalid structured output",
  output_mode_unsupported: "Structured output not supported",
};

export interface CapabilityFailure {
  code: CapabilityFailureCode;
  title: string;
  message: string;
}

function usd(value: unknown): string | null {
  const n = typeof value === "string" || typeof value === "number" ? Number(value) : NaN;
  return Number.isFinite(n) ? `$${n}` : null;
}

/** The turn's failure when it is one of this milestone's codes, in words; else null. */
export function turnCapabilityFailure(events: readonly RunEvent[]): CapabilityFailure | null {
  const failed = events.find((event) => event.type === "run_failed");
  if (!failed) return null;
  const data = failed.data;
  const code = data.error_code;
  if (typeof code !== "string" || !(code in FAILURE_TITLES)) return null;
  const known = code as CapabilityFailureCode;
  let message: string;
  if (known === "usage_limit_exceeded") {
    const limit = typeof data.limit === "string" ? (LIMIT_LABELS[data.limit] ?? data.limit.replace(/_/g, " ")) : null;
    const value = typeof data.value === "number" ? data.value.toLocaleString("en-US") : null;
    message = limit ? `Usage limit exceeded: ${limit}${value ? ` (${value})` : ""}.` : "The run exceeded a usage limit.";
  } else if (known === "run_budget_exceeded") {
    const cap = usd(data.value);
    const spent = usd(data.spent_usd);
    message = cap ? `The run reached its cost limit of ${cap}${spent ? ` (spent ${spent})` : ""}.` : "The run reached its cost limit.";
  } else if (known === "fallback_exhausted") {
    const cause = typeof data.cause === "string" ? data.cause : null;
    message = `Every model of the agent's fallback chain failed${cause ? ` (first failure: ${cause})` : ""}.`;
  } else if (known === "output_invalid") {
    message = "The answer did not match the agent's output schema within the allowed retries.";
  } else {
    message = "This model does not support native structured output. Use tool mode in the agent's Output step.";
  }
  return { code: known, title: FAILURE_TITLES[known], message };
}

// -------------------------------------------------------------------------------- approvals

export type ToolDecisionKind = "approved" | "denied" | "timed_out";

export interface ToolDecision {
  kind: ToolDecisionKind;
  decidedBy: string | null;
  comment: string | null;
}

export interface ToolApprovalState {
  toolCallId: string;
  toolName: string;
  args: unknown;
  deadline: string | null;
  /** Null while the call waits. */
  decision: ToolDecision | null;
}

const DECISION_TYPES: Record<string, ToolDecisionKind> = {
  tool_approved: "approved",
  tool_denied: "denied",
  tool_approval_timed_out: "timed_out",
};

/** Tool call id → its decision, from the decision events (the first one wins, as on the server). */
export function toolDecisions(events: readonly RunEvent[]): Map<string, ToolDecision> {
  const decisions = new Map<string, ToolDecision>();
  for (const event of events) {
    const kind = DECISION_TYPES[event.type];
    const id = event.data.tool_call_id;
    if (!kind || typeof id !== "string" || decisions.has(id)) continue;
    decisions.set(id, {
      kind,
      decidedBy: typeof event.data.decided_by === "string" ? event.data.decided_by : null,
      comment: typeof event.data.comment === "string" && event.data.comment ? event.data.comment : null,
    });
  }
  return decisions;
}

/** Every call of the turn that waited for approval, in order, with its decision if any. */
export function toolApprovals(events: readonly RunEvent[]): ToolApprovalState[] {
  const decisions = toolDecisions(events);
  const out: ToolApprovalState[] = [];
  const seen = new Set<string>();
  for (const event of events) {
    if (event.type !== "tool_approval_waiting") continue;
    const id = event.data.tool_call_id;
    if (typeof id !== "string" || seen.has(id)) continue;
    seen.add(id);
    out.push({
      toolCallId: id,
      toolName: typeof event.data.tool_name === "string" ? event.data.tool_name : "tool",
      args: event.data.args ?? {},
      deadline: typeof event.data.deadline === "string" ? event.data.deadline : null,
      decision: decisions.get(id) ?? null,
    });
  }
  return out;
}

/**
 * The status to show for a turn: the SSE status frame arrives after the event that changed it, so
 * an open approval in the events means "waiting_approval" at once, and a run whose approvals are
 * all decided is "running" again before the next frame (no one-render flash either way). Terminal,
 * queued and recovering statuses are kept as they are.
 */
export function effectiveTurnStatus(status: RunStatus, events: readonly RunEvent[]): RunStatus {
  if (status !== "running" && status !== "waiting_approval") return status;
  if (events.some((event) => event.type === "run_completed" || event.type === "run_failed")) return status;
  const approvals = toolApprovals(events);
  if (approvals.some((approval) => approval.decision === null)) return "waiting_approval";
  return status === "waiting_approval" && approvals.length > 0 ? "running" : status;
}

export const DECISION_LABELS: Record<ToolDecisionKind, string> = {
  approved: "Approved",
  denied: "Denied",
  timed_out: "Timed out (counts as a denial)",
};
