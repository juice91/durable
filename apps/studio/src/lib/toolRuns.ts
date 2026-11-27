// Tool test runs: POST /api/v1/tools/{tool_id}/runs, error mapping, and how a finished run reads.
import { refusedMessage } from "./access";
import { apiHeaders, BASE_URL } from "./api";
import type { Run, RunEvent, RunStatus } from "./runs";

export interface ToolRunAccepted {
  run_id: string;
  kind: "tool";
  tool_id: string;
  status: string;
  display_status: RunStatus;
  trace_id: string | null;
}

/**
 * A refused tool run. `fieldErrors` maps a dotted input path ("options.retries") to its message;
 * `banner` is everything that is not about one input field (allowlist refusal, 404, 409, 503...).
 */
export class ToolRunError extends Error {
  constructor(
    public status: number,
    public banner: string | null,
    public fieldErrors: Record<string, string>,
  ) {
    super(banner ?? Object.values(fieldErrors).join(", ") ?? `HTTP ${status}`);
    this.name = "ToolRunError";
  }
}

interface ValidationItem {
  loc?: unknown;
  msg?: unknown;
}

/** Banners for an error answer without a string detail, by status. */
const TOOL_RUN_FALLBACKS: Record<number, string> = {
  404: "Tool not found",
  409: "This run id is already in use",
  503: "The run could not be started; try again",
};

/** Map an error response body to a banner and per-field messages (pure, for tests). */
export function mapToolRunError(
  status: number,
  body: unknown,
  fallbacks: Record<number, string> = TOOL_RUN_FALLBACKS,
): ToolRunError {
  const detail = body && typeof body === "object" ? (body as { detail?: unknown }).detail : undefined;
  if (typeof detail === "string") return new ToolRunError(status, detail, {});
  if (status === 422 && Array.isArray(detail)) {
    const fieldErrors: Record<string, string> = {};
    const general: string[] = [];
    for (const item of detail as ValidationItem[]) {
      const msg = String(item?.msg ?? "Invalid value");
      const loc = Array.isArray(item?.loc) ? item.loc.map(String) : [];
      // The API reports ["body", "input", ...field path]; anything else is about the request itself.
      const path = loc[0] === "body" && loc[1] === "input" ? loc.slice(2) : [];
      if (path.length === 0) {
        general.push(loc.length > 0 ? `${loc.join(".")}: ${msg}` : msg);
        continue;
      }
      const key = path.join(".");
      fieldErrors[key] = fieldErrors[key] ? `${fieldErrors[key]}; ${msg}` : msg;
    }
    const banner = general.length > 0
      ? general.join("; ")
      : Object.keys(fieldErrors).length > 0 ? "Fix the highlighted input fields" : "The input was rejected";
    return new ToolRunError(status, banner, fieldErrors);
  }
  return new ToolRunError(status, fallbacks[status] ?? `HTTP ${status}`, {});
}

/** `mapToolRunError`, with a refusal of the policy engine said in words. */
export async function toolRunFailure(status: number, body: unknown, fallbacks?: Record<number, string>): Promise<ToolRunError> {
  const refused = await refusedMessage(status, body);
  return refused ? new ToolRunError(status, refused, {}) : mapToolRunError(status, body, fallbacks);
}

export async function startToolRun(
  toolId: string,
  input: Record<string, unknown>,
  runId?: string,
  signal?: AbortSignal,
): Promise<ToolRunAccepted> {
  const body: { input: Record<string, unknown>; run_id?: string } = { input };
  if (runId) body.run_id = runId;
  const response = await fetch(`${BASE_URL}/api/v1/tools/${encodeURIComponent(toolId)}/runs`, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...apiHeaders() },
    body: JSON.stringify(body),
    signal,
  });
  if (!response.ok) {
    let data: unknown = null;
    try {
      data = await response.json();
    } catch {
      // Non-JSON error body: fall back to the status message.
    }
    throw await toolRunFailure(response.status, data);
  }
  return response.json();
}

/** Pretty-print JSON text (a tool run's `output`), or return it as-is when it is not JSON. */
export function prettyJson(text: string | null | undefined): string {
  if (text == null) return "";
  try {
    return JSON.stringify(JSON.parse(text), null, 2);
  } catch {
    return text;
  }
}

export const NEEDS_ATTENTION_MESSAGE = "Needs attention: the tool may have run; check before retrying";

export type ToolRunTone = "pending" | "success" | "error" | "attention";

export interface ToolRunView {
  tone: ToolRunTone;
  status: RunStatus;
  title: string;
  /** Pretty JSON of the result when completed. */
  result: string | null;
  error: string | null;
  errorCode: string | null;
}

const TERMINAL: ReadonlySet<string> = new Set(["completed", "failed", "needs_attention"]);

export function isTerminalStatus(status: string | undefined | null): boolean {
  return Boolean(status && TERMINAL.has(status));
}

/**
 * How a tool run reads in the console, from the run row (when loaded), the latest status frame
 * and the terminal event (which is the only place `error_code` is reported).
 */
export function describeToolRun(
  status: RunStatus,
  run: Pick<Run, "output" | "error"> | null,
  terminalEvent: RunEvent | null,
): ToolRunView {
  const eventData = terminalEvent?.data ?? {};
  const errorCode = typeof eventData.error_code === "string" ? eventData.error_code : null;
  const eventError = typeof eventData.error === "string" ? eventData.error : null;
  // run_failed carries the final status; prefer it when the status frame lags behind.
  const eventStatus = terminalEvent?.type === "run_failed" && typeof eventData.status === "string"
    ? (eventData.status as RunStatus)
    : terminalEvent?.type === "run_completed" ? "completed" : null;
  const effective: RunStatus = eventStatus && isTerminalStatus(eventStatus) ? eventStatus : status;

  if (effective === "needs_attention") {
    return { tone: "attention", status: effective, title: NEEDS_ATTENTION_MESSAGE, result: null, error: run?.error ?? eventError, errorCode };
  }
  if (effective === "failed") {
    return { tone: "error", status: effective, title: "Failed", result: null, error: run?.error ?? eventError ?? "The tool run failed", errorCode };
  }
  if (effective === "completed") {
    const output = run?.output ?? (typeof eventData.output === "string" ? eventData.output : null);
    return { tone: "success", status: effective, title: "Completed", result: output == null ? null : prettyJson(output), error: null, errorCode: null };
  }
  const titles: Record<string, string> = { queued: "Queued", running: "Running", recovering: "Recovering" };
  return { tone: "pending", status: effective, title: titles[effective] ?? effective, result: null, error: null, errorCode: null };
}
