"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ApiError, api } from "@/lib/api";
import { listWorkflows } from "@/lib/workflows";
import { getTrace, type NameMaps, type Trace } from "@/lib/traces";

const TERMINAL = new Set(["completed", "skipped", "failed", "needs_attention"]);
export const TRACE_POLL_MS = 3000;

/** What a poll can change: the run's status and update time, the totals, and each span's id,
 * status, display status (a child run going queued -> running), start and end. Equal signatures mean the poll brought nothing new. */
export function traceSignature(trace: Trace): string {
  const parts = [trace.run.display_status, trace.run.updated_at, String(trace.spans.length), JSON.stringify(trace.totals), JSON.stringify(trace.truncated)];
  for (const span of trace.spans) {
    const display = span.attributes?.display_status ?? "";
    parts.push(`${span.id}|${span.status}|${display}|${span.start ?? ""}|${span.end ?? ""}`);
  }
  return parts.join("\n");
}

/** Agent and workflow names, for span labels. Failures leave the ids. */
export function useNames(): NameMaps {
  const [agentNames, setAgentNames] = useState<Record<string, string>>({});
  const [workflowNames, setWorkflowNames] = useState<Record<string, string>>({});
  useEffect(() => {
    let cancelled = false;
    void Promise.resolve()
      .then(() => api.agents.list())
      .then((agents) => {
        if (!cancelled && Array.isArray(agents)) setAgentNames(Object.fromEntries(agents.map((a) => [a.id, a.name])));
      })
      .catch(() => undefined);
    void Promise.resolve()
      .then(() => listWorkflows())
      .then((workflows) => {
        if (!cancelled && Array.isArray(workflows)) setWorkflowNames(Object.fromEntries(workflows.map((w) => [w.id, w.name])));
      })
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, []);
  return useMemo(() => ({ agentNames, workflowNames }), [agentNames, workflowNames]);
}

export interface TraceState {
  trace: Trace | null;
  /** The last load's error ("Run not found" or "Failed to load the trace"); null after a success. */
  error: string | null;
  /** The selected span (the root after the first load; kept across polls while it exists). */
  selectedId: string | null;
  setSelectedId: (spanId: string) => void;
  /** Load again now (Retry after a failure). */
  reload: () => void;
}

/** One run's trace from GET /runs/:id/trace, polled every TRACE_POLL_MS while the run is not finished. */
export function useTrace(runId: string, initialSpanId: string | null = null): TraceState {
  const [trace, setTrace] = useState<Trace | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [selectedId, setSelectedId] = useState<string | null>(null);

  // Bumped after every load, success or failure, so a failed poll still schedules the next one.
  const [loadTick, setLoadTick] = useState(0);
  const signatureRef = useRef<string | null>(null);

  // The run on screen: a response for a run the view has since moved away from is dropped.
  const runIdRef = useRef(runId);
  runIdRef.current = runId;
  // The span the Sheet was opened on (`?span=`), selected on the first load when the trace has it.
  const initialSpanRef = useRef(initialSpanId);
  initialSpanRef.current = initialSpanId;

  const load = useCallback(async () => {
    const requested = runId;
    try {
      const data = await getTrace(requested);
      if (requested !== runIdRef.current) return;
      const signature = traceSignature(data);
      // An unchanged poll keeps the same trace object, so nothing below re-renders.
      if (signature !== signatureRef.current) {
        signatureRef.current = signature;
        setTrace(data);
        setSelectedId((prev) => {
          if (prev && data.spans.some((s) => s.id === prev)) return prev;
          const wanted = initialSpanRef.current;
          return wanted && data.spans.some((s) => s.id === wanted) ? wanted : (data.spans[0]?.id ?? null);
        });
      }
      setError(null);
    } catch (err) {
      if (requested !== runIdRef.current) return;
      setError(err instanceof ApiError && err.status === 404 ? "Run not found" : "Failed to load the trace");
    } finally {
      if (requested === runIdRef.current) setLoadTick((t) => t + 1);
    }
  }, [runId]);

  useEffect(() => {
    signatureRef.current = null;
    setTrace(null);
    setError(null);
    setSelectedId(null);
    void load();
  }, [load]);

  const running = trace !== null && !TERMINAL.has(trace.run.display_status);
  useEffect(() => {
    if (!running || loadTick === 0) return;
    const timer = setTimeout(() => void load(), TRACE_POLL_MS);
    return () => clearTimeout(timer);
  }, [running, loadTick, load]);

  const reload = useCallback(() => void load(), [load]);
  return { trace, error, selectedId, setSelectedId, reload };
}
