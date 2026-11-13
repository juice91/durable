// What a tool-call card shows besides the call itself: its status and how long it took, read from
// the turn's run events (the call is `tool_call`, or a `model_message` that embeds it; the outcome is
// `tool_result` with `status` "ok", "error" or "no_result").
import type { RunEvent } from "@/lib/runs";

/** The run a thread message belongs to (message ids are `<runId>:...`), or null. */
export function messageRunId(messageId: string): string | null {
  const colon = messageId.indexOf(":");
  return colon > 0 ? messageId.slice(0, colon) : null;
}

/** The key of one tool call in the chat's info map: a call id is unique only within its run. */
export function toolCallInfoKey(runId: string, toolCallId: string): string {
  return `${runId}:${toolCallId}`;
}

export type ToolCallStatus = "running" | "ok" | "error" | "no_result";

export interface ToolCallInfo {
  status: ToolCallStatus;
  /** Milliseconds from the call event to its result event; null while unknown. */
  durationMs: number | null;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function time(ts: string): number | null {
  const value = Date.parse(ts);
  return Number.isNaN(value) ? null : value;
}

/**
 * Status and duration per tool call id. A call without a result is "running" while the turn runs
 * and "no_result" once it has ended.
 */
export function toolCallInfos(events: readonly RunEvent[], turnRunning: boolean): Map<string, ToolCallInfo> {
  const started = new Map<string, number | null>();
  const results = new Map<string, { status: ToolCallStatus; at: number | null }>();
  for (const event of events) {
    if (event.type === "tool_call" && typeof event.data.tool_call_id === "string") {
      if (!started.has(event.data.tool_call_id)) started.set(event.data.tool_call_id, time(event.ts));
    } else if (event.type === "model_message" && Array.isArray(event.data.tool_calls)) {
      for (const call of event.data.tool_calls) {
        if (isRecord(call) && typeof call.tool_call_id === "string" && !started.has(call.tool_call_id)) {
          started.set(call.tool_call_id, time(event.ts));
        }
      }
    } else if (event.type === "tool_result" && typeof event.data.tool_call_id === "string") {
      const raw = event.data.status;
      const status: ToolCallStatus = raw === "error" ? "error" : raw === "no_result" ? "no_result" : "ok";
      results.set(event.data.tool_call_id, { status, at: time(event.ts) });
    }
  }
  const infos = new Map<string, ToolCallInfo>();
  for (const id of new Set([...started.keys(), ...results.keys()])) {
    const result = results.get(id);
    const start = started.get(id) ?? null;
    if (!result) {
      infos.set(id, { status: turnRunning ? "running" : "no_result", durationMs: null });
      continue;
    }
    const duration = start !== null && result.at !== null && result.at >= start ? result.at - start : null;
    infos.set(id, { status: result.status, durationMs: duration });
  }
  return infos;
}

/** "120 ms", "1.4 s", "2 min 5 s". */
export function formatDuration(ms: number): string {
  if (ms < 1000) return `${Math.round(ms)} ms`;
  if (ms < 60_000) return `${(ms / 1000).toFixed(ms < 10_000 ? 1 : 0)} s`;
  const minutes = Math.floor(ms / 60_000);
  const seconds = Math.round((ms % 60_000) / 1000);
  return seconds ? `${minutes} min ${seconds} s` : `${minutes} min`;
}

export const TOOL_STATUS_LABELS: Record<ToolCallStatus, string> = {
  running: "Running",
  ok: "Done",
  error: "Failed",
  no_result: "No result",
};
