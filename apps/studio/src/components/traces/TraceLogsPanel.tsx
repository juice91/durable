"use client";

import Link from "next/link";
import React, { useMemo, useState } from "react";
import { spanLabel } from "@/lib/traces";
import { LogsTable } from "@/components/logs/LogsTable";
import { LogsUnavailable } from "@/components/logs/LogsUnavailable";
import { logsPath, MAX_LOG_LIMIT, traceLogsParams, type RunTimes } from "@/components/logs/logsQuery";
import { useLogSearch } from "@/components/logs/useLogSearch";
import type { TraceRun, TraceSpan } from "@/lib/traces";

/** What the logs panel (P0.20) gets: the run, its OpenTelemetry trace id, the selected span and
 * that span's time window (ISO; `end` null while the span is open). */
export interface TraceLogsSlotProps {
  runId: string;
  run: TraceRun;
  traceId: string | null;
  selectedSpan: TraceSpan | null;
  window: { start: string | null; end: string | null };
}

/** The trace view's logs panel (P0.20): the run's logs from GET /logs, narrowed to the selected
 * span's time window, or all of the run's logs. Oldest first, like the waterfall. */
export function TraceLogsPanel({
  runId,
  run = null,
  selectedSpan,
  window,
}: Pick<TraceLogsSlotProps, "runId" | "selectedSpan" | "window"> & { run?: RunTimes | null }) {
  const [showAll, setShowAll] = useState(false);
  const [nonce, setNonce] = useState(0);

  const canNarrow = !!selectedSpan && !!window.start;
  const narrow = canNarrow && !showAll;
  // Everything the query depends on, as strings (a trace poll hands over new but equal objects).
  // The whole-run window is computed when the query is sent: a run still going ends at now.
  const runKey = run ? [run.created_at, run.finished_at ?? "", run.updated_at ?? "", run.display_status ?? ""].join("|") : "";
  const key = JSON.stringify([runId, narrow ? window.start : null, narrow ? window.end : null, narrow ? "" : runKey, nonce]);
  const search = useLogSearch(key, () => traceLogsParams(runId, window, narrow, MAX_LOG_LIMIT, run));
  const limit = MAX_LOG_LIMIT;

  const items = useMemo(() => [...search.items].reverse(), [search.items]);
  const spanName = selectedSpan ? spanLabel(selectedSpan, {}, undefined) : null;
  const scope = narrow ? `during ${spanName}` : "for the whole run";
  const truncated = search.items.length >= limit;

  return (
    <div className="space-y-3 rounded-md border border-border bg-card p-4" data-testid="trace-logs-panel">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div>
          <h2 className="text-sm font-semibold text-card-foreground">Logs</h2>
          <p className="text-xs text-muted-foreground" data-testid="trace-logs-scope">
            {narrow ? <>Records logged during the selected span: <span className="font-medium">{spanName}</span></> : "All records logged by this run"}
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          {canNarrow ? (
            <button
              type="button"
              aria-pressed={showAll}
              onClick={() => setShowAll((v) => !v)}
              className="inline-flex items-center rounded-md bg-muted px-3 py-1.5 text-xs font-medium text-card-foreground hover:bg-muted/80 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            >
              {showAll ? "Only the selected span" : "Show all run logs"}
            </button>
          ) : null}
          <button
            type="button"
            onClick={() => setNonce((n) => n + 1)}
            disabled={search.loading}
            className="inline-flex items-center rounded-md bg-muted px-3 py-1.5 text-xs font-medium text-card-foreground hover:bg-muted/80 disabled:opacity-50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          >
            Refresh
          </button>
          <Link
            href={logsPath({ run_id: runId })}
            className="text-xs text-primary hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          >
            Search in Logs
          </Link>
        </div>
      </div>

      <p className="sr-only" aria-live="polite">
        {search.loading ? "Loading logs" : search.error ? "" : `${items.length} log record${items.length === 1 ? "" : "s"} ${scope}`}
      </p>

      {search.error ? (
        <LogsUnavailable error={search.error} onRetry={() => setNonce((n) => n + 1)} compact />
      ) : search.loading && items.length === 0 ? (
        <p className="py-4 text-center text-sm text-muted-foreground">Loading logs...</p>
      ) : items.length === 0 ? (
        <p className="py-4 text-center text-sm text-muted-foreground" data-testid="trace-logs-empty">
          {narrow ? "No logs during this span." : "No logs for this run."}
          {narrow ? (
            <>
              {" "}
              <button type="button" onClick={() => setShowAll(true)} className="text-primary hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring">
                Show all run logs
              </button>
            </>
          ) : null}
        </p>
      ) : (
        <>
          <LogsTable items={items} caption={`Logs ${scope}`} showTraceLink={false} busy={search.loading} />
          {truncated ? (
            <p className="text-xs text-muted-foreground">
              Showing the newest {limit} records.{" "}
              <Link href={logsPath({ run_id: runId })} className="text-primary hover:underline">Search all in Logs</Link>
            </p>
          ) : null}
        </>
      )}
    </div>
  );
}
