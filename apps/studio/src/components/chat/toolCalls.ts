// What a tool-call card shows besides the call itself: its status and how long it took, read from
// the turn's run events (the call is `tool_call`, or a `model_message` that embeds it; the outcome is
// `tool_result` with `status` "ok", "error" or "no_result").
import type { RunEvent } from "@/lib/runs";
import { parseSources, RETRIEVE_TOOL_NAME, type CitationSource } from "./citations";

/** The run a thread message belongs to (message ids are `<runId>:...`), or null. */
export function messageRunId(messageId: string): string | null {
  const colon = messageId.indexOf(":");
  return colon > 0 ? messageId.slice(0, colon) : null;
}

/** The key of one tool call in the chat's info map: a call id is unique only within its run. */
export function toolCallInfoKey(runId: string, toolCallId: string): string {
  return `${runId}:${toolCallId}`;
}

/** `waiting`: a gated call waits for a person (AC.6); `denied`: denied or timed out, it never ran. */
export type ToolCallStatus = "running" | "waiting" | "ok" | "error" | "no_result" | "denied";

export interface ToolCallInfo {
  status: ToolCallStatus;
  /** Milliseconds from the call event to its result event; null while unknown. */
  durationMs: number | null;
  /**
   * A `retrieve` call's structured sources from its `tool_result` event (`[]` when it returned none,
   * failed, or has no result yet: the turn called retrieve all the same, so a link the model wrote
   * as `[n](url)` is never trusted); absent for every other tool. Citation links in the answer come only from these (RM.16).
   */
  sources?: CitationSource[];
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
  const results = new Map<string, { status: ToolCallStatus; at: number | null; sources?: CitationSource[] }>();
  // Calls of the retrieve tool, known from the call (its result may not have come yet).
  const retrieveCalls = new Set<string>();
  const noteRetrieve = (id: string, name: unknown) => {
    if (name === RETRIEVE_TOOL_NAME) retrieveCalls.add(id);
  };
  for (const event of events) {
    if (event.type === "tool_call" && typeof event.data.tool_call_id === "string") {
      if (!started.has(event.data.tool_call_id)) started.set(event.data.tool_call_id, time(event.ts));
      noteRetrieve(event.data.tool_call_id, event.data.tool_name);
    } else if (event.type === "model_message" && Array.isArray(event.data.tool_calls)) {
      for (const call of event.data.tool_calls) {
        if (isRecord(call) && typeof call.tool_call_id === "string") {
          if (!started.has(call.tool_call_id)) started.set(call.tool_call_id, time(event.ts));
          noteRetrieve(call.tool_call_id, call.tool_name);
        }
      }
    } else if (event.type === "tool_result" && typeof event.data.tool_call_id === "string") {
      const raw = event.data.status;
      const status: ToolCallStatus = raw === "error" ? "error" : raw === "no_result" ? "no_result" : raw === "denied" ? "denied" : "ok";
      const retrieve = event.data.tool_name === RETRIEVE_TOOL_NAME || retrieveCalls.has(event.data.tool_call_id);
      const sources = retrieve ? parseSources(event.data.sources) : undefined;
      results.set(event.data.tool_call_id, { status, at: time(event.ts), ...(sources ? { sources } : {}) });
    }
  }
  // AC.6: a call waiting for approval (no decision yet) is "waiting"; a denied one that has no
  // result yet is "denied" already.
  const awaiting = new Set<string>();
  const refused = new Set<string>();
  for (const event of events) {
    const id = event.data.tool_call_id;
    if (typeof id !== "string") continue;
    if (event.type === "tool_approval_waiting") awaiting.add(id);
    else if (event.type === "tool_approved") awaiting.delete(id);
    else if (event.type === "tool_denied" || event.type === "tool_approval_timed_out") {
      awaiting.delete(id);
      refused.add(id);
    }
  }
  const infos = new Map<string, ToolCallInfo>();
  for (const id of new Set([...started.keys(), ...results.keys()])) {
    const result = results.get(id);
    const start = started.get(id) ?? null;
    if (!result) {
      const pending: ToolCallStatus = refused.has(id) ? "denied" : awaiting.has(id) && turnRunning ? "waiting" : turnRunning ? "running" : "no_result";
      infos.set(id, { status: pending, durationMs: null, ...(retrieveCalls.has(id) ? { sources: [] } : {}) });
      continue;
    }
    const duration = start !== null && result.at !== null && result.at >= start ? result.at - start : null;
    infos.set(id, { status: result.status, durationMs: duration, ...(result.sources ? { sources: result.sources } : {}) });
  }
  return infos;
}

/** The shared duration format (`lib/format.ts`). */
export { formatDuration } from "@/lib/format";

export const TOOL_STATUS_LABELS: Record<ToolCallStatus, string> = {
  running: "Running",
  waiting: "Waiting for approval",
  denied: "Denied",
  ok: "Done",
  error: "Failed",
  no_result: "No result",
};
