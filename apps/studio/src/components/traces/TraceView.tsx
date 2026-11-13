"use client";

import Link from "next/link";
import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ApiError, api } from "@/lib/api";
import { buildLogsLink, buildTraceLink } from "@/lib/grafana";
import { listWorkflows } from "@/lib/workflows";
import {
  flattenSpans,
  getTrace,
  spanLabel,
  traceWindow,
  type NameMaps,
  type Trace,
  type TraceRun,
  type TraceSpan,
} from "@/lib/traces";
import { StatusPill } from "@/components/runs/StatusPill";
import { SpanDetail } from "./SpanDetail";
import { renderTraceLogsPanel } from "./TraceLogsPanel";
import { TraceTotals } from "./TraceTotals";
import { TraceWaterfall } from "./TraceWaterfall";

const TERMINAL = new Set(["completed", "failed", "needs_attention"]);
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

/** What the logs panel (P0.20) gets: the run, its OpenTelemetry trace id, the selected span and
 * that span's time window (ISO; `end` null while the span is open). */
export interface TraceLogsSlotProps {
  runId: string;
  run: TraceRun;
  traceId: string | null;
  selectedSpan: TraceSpan | null;
  window: { start: string | null; end: string | null };
}

interface TraceViewProps {
  runId: string;
  /** Inside the run page: no page header (the page has its own). */
  embedded?: boolean;
  /** The trace view's logs panel; defaults to the run's logs from GET /logs (P0.20), `null` hides it. */
  renderLogsPanel?: ((props: TraceLogsSlotProps) => React.ReactNode) | null;
}

function useNames(): NameMaps {
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

/** The built-in trace of one run: totals, truncation notices, the span waterfall and the selected
 * span's detail. Polls while the run is not finished. */
export function TraceView({ runId, embedded = false, renderLogsPanel = renderTraceLogsPanel }: TraceViewProps) {
  const [trace, setTrace] = useState<Trace | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [collapsed, setCollapsed] = useState<Set<string>>(() => new Set());
  const names = useNames();

  // Bumped after every load, success or failure, so a failed poll still schedules the next one.
  const [loadTick, setLoadTick] = useState(0);
  const signatureRef = useRef<string | null>(null);

  // The run on screen: a response for a run the view has since moved away from is dropped.
  const runIdRef = useRef(runId);
  runIdRef.current = runId;

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
        setSelectedId((prev) => (prev && data.spans.some((s) => s.id === prev) ? prev : (data.spans[0]?.id ?? null)));
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
    setCollapsed(new Set());
    void load();
  }, [load]);

  const running = trace !== null && !TERMINAL.has(trace.run.display_status);
  useEffect(() => {
    if (!running || loadTick === 0) return;
    const timer = setTimeout(() => void load(), TRACE_POLL_MS);
    return () => clearTimeout(timer);
  }, [running, loadTick, load]);

  const rows = useMemo(() => (trace ? flattenSpans(trace.spans, collapsed) : []), [trace, collapsed]);
  const window = useMemo(() => (trace ? traceWindow(trace.spans) : null), [trace]);
  const selected = trace?.spans.find((s) => s.id === selectedId) ?? null;

  const toggle = useCallback((spanId: string) => {
    setCollapsed((prev) => {
      const next = new Set(prev);
      if (next.has(spanId)) next.delete(spanId);
      else next.add(spanId);
      return next;
    });
  }, []);

  if (error && !trace) {
    return (
      <div role="alert" className="rounded-md border border-border bg-card p-6 text-center text-sm text-muted-foreground">
        {error}
      </div>
    );
  }
  if (!trace) {
    return <p className="py-8 text-center text-sm text-muted-foreground">Loading trace...</p>;
  }

  const run = trace.run;
  const traceId = run.trace_id ?? null;
  const root = trace.spans[0] ?? null;
  const t = trace.truncated;
  const notices: string[] = [];
  if (t.spans) notices.push(`Only the first ${trace.limits.max_spans.toLocaleString("en-US")} spans are shown.`);
  if (t.events) notices.push(`A run has more than ${trace.limits.max_events_per_run.toLocaleString("en-US")} events; later events are not shown.`);
  if (t.child_runs) notices.push(`Only the first ${trace.limits.max_child_runs.toLocaleString("en-US")} child runs are shown.`);
  if (t.payload_fields > 0) {
    notices.push(`${t.payload_fields} input or output field${t.payload_fields === 1 ? " was" : "s were"} cut to a preview (over ${Math.round(trace.limits.field_max_bytes / 1024)} KB, or the trace's payload budget was spent).`);
  }

  return (
    <div className="space-y-4" data-testid="trace-view">
      {!embedded ? (
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div>
            <Link href="/traces" className="text-sm text-muted-foreground hover:text-card-foreground">← Traces</Link>
            <h1 className="mt-1 text-2xl font-bold text-card-foreground">{root ? spanLabel(root, names) : `Run ${run.run_id}`}</h1>
            <p className="mt-1 font-mono text-xs text-muted-foreground">Run {run.run_id}</p>
          </div>
          <div className="flex items-center gap-2">
            <a
              href={`/runs/${encodeURIComponent(run.run_id)}`}
              className="inline-flex items-center rounded-md bg-muted px-3 py-1.5 text-xs font-medium text-muted-foreground hover:bg-muted/80"
            >
              Run details
            </a>
            {traceId ? (
              <>
                <a
                  href={buildTraceLink(traceId)}
                  target="_blank"
                  rel="noopener noreferrer"
                  data-testid="trace-grafana-link"
                  className="inline-flex items-center rounded-md bg-muted px-3 py-1.5 text-xs font-medium text-muted-foreground hover:bg-muted/80"
                >
                  Open in Grafana
                </a>
                <a
                  href={buildLogsLink(traceId)}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="inline-flex items-center rounded-md bg-muted px-3 py-1.5 text-xs font-medium text-muted-foreground hover:bg-muted/80"
                >
                  Grafana logs
                </a>
              </>
            ) : null}
            <StatusPill status={run.display_status} />
          </div>
        </div>
      ) : null}

      <TraceTotals totals={trace.totals} />

      {notices.length ? (
        <div role="status" data-testid="trace-truncation" className="rounded-md border border-border bg-muted/50 p-3 text-sm text-muted-foreground">
          <p className="font-medium text-card-foreground">This trace is truncated.</p>
          <ul className="mt-1 list-disc pl-5">
            {notices.map((n) => (
              <li key={n}>{n}</li>
            ))}
          </ul>
        </div>
      ) : null}
      {error ? <p role="alert" className="text-sm text-muted-foreground">{error}; showing the last loaded trace.</p> : null}

      <div className="grid gap-4 lg:grid-cols-5">
        <div className="lg:col-span-3">
          <TraceWaterfall
            rows={rows}
            window={window}
            selectedId={selectedId}
            onSelect={setSelectedId}
            collapsed={collapsed}
            onToggle={toggle}
            names={names}
          />
        </div>
        <div className="lg:col-span-2">
          <SpanDetail span={selected} names={names} />
        </div>
      </div>

      {/* Logs slot (P0.20): the run's logs, narrowed to the selected span's time window. */}
      {renderLogsPanel ? (
        <section aria-label="Logs" data-testid="trace-logs-slot">
          {renderLogsPanel({
            runId: run.run_id,
            run,
            traceId,
            selectedSpan: selected,
            window: { start: selected?.start ?? null, end: selected?.end ?? null },
          })}
        </section>
      ) : null}
    </div>
  );
}
