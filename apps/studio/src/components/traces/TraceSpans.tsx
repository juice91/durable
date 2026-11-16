"use client";

import React, { useCallback, useMemo, useState } from "react";
import { cn } from "@/lib/cn";
import { flattenSpans, traceWindow, type NameMaps, type Trace } from "@/lib/traces";
import { SpanDetail } from "./SpanDetail";
import { TraceTotals } from "./TraceTotals";
import { TraceWaterfall, type SpanTreeMode } from "./TraceWaterfall";
import { SPAN_GRID_BESIDE_CLASS } from "./traceSheetModel";

/** The truncation notices of a trace, as sentences. */
export function truncationNotices(trace: Trace): string[] {
  const t = trace.truncated;
  const notices: string[] = [];
  if (t.spans) notices.push(`Only the first ${trace.limits.max_spans.toLocaleString("en-US")} spans are shown.`);
  if (t.events) notices.push(`A run has more than ${trace.limits.max_events_per_run.toLocaleString("en-US")} events; later events are not shown.`);
  if (t.child_runs) notices.push(`Only the first ${trace.limits.max_child_runs.toLocaleString("en-US")} child runs are shown.`);
  if (t.payload_fields > 0) {
    notices.push(`${t.payload_fields} input or output field${t.payload_fields === 1 ? " was" : "s were"} cut to a preview (over ${Math.round(trace.limits.field_max_bytes / 1024)} KB, or the trace's payload budget was spent).`);
  }
  return notices;
}

interface TraceSpansProps {
  trace: Trace;
  names: NameMaps;
  selectedId: string | null;
  onSelect: (spanId: string) => void;
  /** The last poll's error, shown above the spans while the last loaded trace stays. */
  error?: string | null;
  /** Timeline (bars on the time axis) or Tree (the span tree alone). Default timeline. */
  mode?: SpanTreeMode;
  /** The span detail beside the spans (the Sheet decided both fit: `spanDetailBeside`) or below them. */
  detailBeside?: boolean;
}

/** A trace's totals, truncation notices, span waterfall (or tree) and the selected span's detail. */
export function TraceSpans({ trace, names, selectedId, onSelect, error = null, mode = "timeline", detailBeside = false }: TraceSpansProps) {
  const [collapsed, setCollapsed] = useState<Set<string>>(() => new Set());
  const rows = useMemo(() => flattenSpans(trace.spans, collapsed), [trace, collapsed]);
  const window = useMemo(() => traceWindow(trace.spans), [trace]);
  const selected = trace.spans.find((s) => s.id === selectedId) ?? null;
  const notices = truncationNotices(trace);

  const toggle = useCallback((spanId: string) => {
    setCollapsed((prev) => {
      const next = new Set(prev);
      if (next.has(spanId)) next.delete(spanId);
      else next.add(spanId);
      return next;
    });
  }, []);

  return (
    <>
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

      <div
        data-testid="trace-spans-layout"
        data-detail-beside={String(detailBeside)}
        className={cn("grid min-w-0 gap-4", detailBeside ? SPAN_GRID_BESIDE_CLASS : "grid-cols-1")}
      >
        <div className="min-w-0">
          <TraceWaterfall
            rows={rows}
            window={window}
            selectedId={selectedId}
            onSelect={onSelect}
            collapsed={collapsed}
            onToggle={toggle}
            names={names}
            mode={mode}
          />
        </div>
        <div className="min-w-0">
          <SpanDetail span={selected} names={names} runId={trace.run.run_id} />
        </div>
      </div>
    </>
  );
}
