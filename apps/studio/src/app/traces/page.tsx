"use client";

import { TRACE_COLUMNS_STORAGE_KEY } from "@/lib/traces";
import React, { Suspense, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { formatCost, formatTokens } from "@/lib/format";
import { useSearchParams } from "next/navigation";
import { StatusPill } from "@/components/runs/StatusPill";
import { buildTraceLink, buildLogsLink } from "@/lib/grafana";
import { api } from "@/lib/api";
import { listWorkflows } from "@/lib/workflows";
import { RunSubject } from "@/components/runs/RunSubject";
import { useKnowledgeBaseNames } from "@/components/knowledge/useKnowledgeBaseNames";
import {
  EMPTY_FILTERS,
  formatDuration,
  hasActiveFilters,
  listTraceRuns,
  hasRunOnlyFilters,
  parseTraceFilters,
  runDurationMs,
  traceFiltersToParams,
  type TraceFilters,
  type TraceRun,
} from "@/lib/traces";
import { routes } from "@/lib/routes";
import { PageHeader } from "@/components/frame/PageHeader";
import { navCrumb } from "@/lib/nav";
import { DataList, type DataListColumn } from "@/components/shared/DataList";
import { Button } from "@/components/ui/Button";
import { ColumnChooser, useColumnVisibility } from "@/components/shared/ColumnChooser";
import { RunModel } from "@/components/traces/RunModel";
import { TraceLink } from "@/components/traces/TraceLink";
import { TraceSheetHost } from "@/components/traces/TraceSheetHost";
import { usePendingQuery } from "@/components/traces/usePendingQuery";
import { TraceFilterBar } from "@/components/traces/TraceFilterBar";
import { TracesTabs } from "@/components/traces/TracesTabs";
import { RunsHistogram } from "@/components/traces/RunsHistogram";
import { ExternalTraceSheet } from "@/components/traces/ExternalTraceSheet";
import { Badge } from "@/components/ui/Badge";
import { externalDurationMs, listExternalTraces, type ExternalTrace } from "@/lib/externalTraces";

/** The list row's test id (kept from P0.13), as a `testId` key so the test-id inventory sees it rendered. */
const TRACE_ROW = { testId: "trace-row" } as const;

const TERMINAL: ReadonlySet<string> = new Set(["completed", "failed", "needs_attention"]);

/** Per-viewer column choice (hidden ids) for the list; tags are off until asked for. */
const DEFAULT_HIDDEN_COLUMNS = ["tags"] as const;
const dim = "text-muted-foreground";
const linkClass = "text-info-text underline-offset-4 hover:underline";

function TracesList() {
  const searchParams = useSearchParams();
  // Only the filters: opening, switching or closing the trace Sheet (`?trace=`, `&tab=`) must not
  // reload the list.
  const filterKey = traceFiltersToParams(parseTraceFilters(new URLSearchParams(searchParams?.toString() ?? ""))).toString();
  const filters = useMemo(() => parseTraceFilters(new URLSearchParams(filterKey)), [filterKey]);

  const [runs, setRuns] = useState<TraceRun[]>([]);
  const [nextBefore, setNextBefore] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  // A first-page load is in flight (the rows shown, if any, are for the previous filters).
  const [reloading, setReloading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [agentNames, setAgentNames] = useState<Record<string, string>>({});
  const [workflowNames, setWorkflowNames] = useState<Record<string, string>>({});

  useEffect(() => {
    let cancelled = false;
    void api.agents.list().then((agents) => {
      if (!cancelled) {
        setAgentNames(Object.fromEntries(agents.map((agent) => [agent.id, agent.name])));
      }
    }).catch(() => undefined);
    void Promise.resolve()
      .then(() => listWorkflows())
      .then((workflows) => {
        if (!cancelled && Array.isArray(workflows)) {
          setWorkflowNames(Object.fromEntries(workflows.map((workflow) => [workflow.id, workflow.name])));
        }
      })
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, []);

  // kb_ingest rows are labelled with their knowledge base's name (fetched only when one is listed).
  const knowledgeBaseNames = useKnowledgeBaseNames(runs.some((run) => run.kind === "kb_ingest"));

  // The rows' ids as the list shows them, for the trace Sheet's prev / next.
  const [visibleIds, setVisibleIds] = useState<string[] | null>(null);

  // Filter writes, kept ahead of the URL while in flight (usePendingQuery): a text filter committed on
  // blur by the click that opens the trace Sheet is kept (the Sheet builds on it), and filter writes
  // keep an open Sheet's `trace` and `tab`.
  const { setFilters, writeFilters, pendingParams } = usePendingQuery<TraceFilters>({
    filters,
    toParams: traceFiltersToParams,
    href: routes.traces,
  });

  // Every request (first page or "Load more") takes a new generation; a response is applied only
  // while its generation is still the latest, so a slow page for old filters (or an old cursor)
  // never overwrites or extends newer results.
  const requestRef = useRef(0);

  // External OpenTelemetry traces (Source: External or All). Run-only filters (status, agent, model, tags...)
  // match none of them, so they are not asked for then.
  const showRuns = filters.source !== "external";
  const showExternal = filters.source !== "platform" && !hasRunOnlyFilters(filters);
  const [external, setExternal] = useState<ExternalTrace[]>([]);
  const [externalNext, setExternalNext] = useState<string | null>(null);
  const [externalError, setExternalError] = useState<string | null>(null);
  const [openExternal, setOpenExternal] = useState<string | null>(searchParams?.get("external") || null);
  const externalRequestRef = useRef(0);
  const loadExternal = useCallback(async (before?: string) => {
    const request = ++externalRequestRef.current;
    if (!showExternal) {
      setExternal([]);
      setExternalNext(null);
      setExternalError(null);
      return;
    }
    try {
      const result = await listExternalTraces(filters, { limit: 50, before });
      if (request !== externalRequestRef.current) return;
      setExternal((prev) => (before ? [...prev, ...result.items] : result.items));
      setExternalNext(result.next_before);
      setExternalError(null);
    } catch (err) {
      if (request !== externalRequestRef.current) return;
      setExternalError(err instanceof Error && err.message ? err.message : "Failed to load external traces");
      if (!before) {
        setExternal([]);
        setExternalNext(null);
      }
    }
  }, [filters, showExternal]);
  useEffect(() => {
    void loadExternal();
  }, [loadExternal]);

  const loadRuns = useCallback(async (before?: string) => {
    const request = ++requestRef.current;
    if (!showRuns) {
      setRuns([]);
      setNextBefore(null);
      setError(null);
      setLoading(false);
      setReloading(false);
      return;
    }
    setLoading(true);
    if (!before) {
      // A new first page: the old cursor and error belong to the old filters.
      setNextBefore(null);
      setError(null);
      setReloading(true);
    }
    try {
      const result = await listTraceRuns(filters, { limit: 50, before });
      if (request !== requestRef.current) return;
      setRuns((prev) => {
        const newRuns = before ? [...prev, ...result.items] : result.items;
        return [...newRuns].sort((a, b) => new Date(b.created_at).getTime() - new Date(a.created_at).getTime());
      });
      setNextBefore(result.next_before);
      setError(null);
    } catch (err) {
      if (request !== requestRef.current) return;
      setError(err instanceof Error && err.message ? err.message : "Failed to load runs");
      if (!before) {
        setRuns([]);
        setNextBefore(null);
      }
    } finally {
      if (request === requestRef.current) {
        setLoading(false);
        setReloading(false);
      }
    }
  }, [filters, showRuns]);

  useEffect(() => {
    void loadRuns();
  }, [loadRuns]);


  const externalColumns: DataListColumn<ExternalTrace>[] = [
    { id: "time", header: "Time", cell: (trace) => <span className="whitespace-nowrap font-mono text-caption">{new Date(trace.start).toLocaleString()}</span> },
    {
      id: "service",
      header: "Service",
      cell: (trace) => <Badge variant="info" data-testid="external-trace-service-badge">{trace.service_name ?? "unknown service"}</Badge>,
    },
    { id: "name", header: "Trace", cell: (trace) => <span className="font-medium">{trace.name ?? "(unnamed)"}</span> },
    { id: "status", header: "Status", cell: (trace) => <Badge variant={trace.status === "error" ? "destructive" : "success"}>{trace.status === "error" ? "error" : "ok"}</Badge> },
    { id: "spans", header: "Spans", align: "end", cell: (trace) => <span className="font-mono text-caption">{trace.span_count}</span> },
    { id: "duration", header: "Duration", align: "end", cell: (trace) => <span className={`whitespace-nowrap ${dim}`}>{formatDuration(externalDurationMs(trace))}</span> },
    { id: "trace_id", header: "Trace id", cell: (trace) => <span className="font-mono text-caption">{trace.trace_id.slice(0, 8)}</span> },
  ];

  const allColumns: DataListColumn<TraceRun>[] = [
    {
      id: "time",
      header: "Time",
      cell: (run) => <span className="whitespace-nowrap font-mono text-caption">{new Date(run.created_at).toLocaleString()}</span>,
    },
    {
      id: "subject",
      header: "Agent / Tool / Workflow",
      cell: (run) => (
        <span className="font-medium">
          <RunSubject run={run} agentNames={agentNames} workflowNames={workflowNames} knowledgeBaseNames={knowledgeBaseNames} />
        </span>
      ),
    },
    { id: "status", header: "Status", cell: (run) => <StatusPill status={run.display_status} /> },
    {
      id: "tags",
      header: "Tags",
      cell: (run) =>
        run.tags && run.tags.length ? (
          <span className="flex flex-wrap gap-1" data-testid="trace-row-tags">
            {run.tags.map((tag) => <span key={tag} className="rounded-sm border border-border px-1 font-mono text-caption">{tag}</span>)}
          </span>
        ) : (
          <span className={dim}>-</span>
        ),
    },
    {
      id: "model",
      header: "Model",
      cell: (run) => (
        <span className="font-mono text-caption" data-testid="trace-row-model">
          <RunModel run={run} />
          {run.model_overridden ? <span className={`ml-1 ${dim}`} title="Model overridden for this run">(override)</span> : null}
        </span>
      ),
    },
    {
      id: "tokens",
      header: "Tokens (in / out)",
      align: "end",
      cell: (run) => {
        const hasUsage = run.input_tokens != null || run.output_tokens != null;
        return (
          <span className="whitespace-nowrap font-mono text-caption" data-testid="trace-row-tokens">
            {hasUsage
              ? `${formatTokens(run.input_tokens)} / ${formatTokens(run.output_tokens)}`
              : <span className={dim}>{TERMINAL.has(run.display_status) ? "not recorded" : "-"}</span>}
          </span>
        );
      },
    },
    {
      id: "cost",
      header: "Cost",
      align: "end",
      cell: (run) => {
        const hasUsage = run.input_tokens != null || run.output_tokens != null;
        const cost = formatCost(run.cost_usd);
        return (
          <span className="whitespace-nowrap font-mono text-caption" data-testid="trace-row-cost">
            {cost ?? <span className={dim}>{hasUsage ? "no price" : TERMINAL.has(run.display_status) ? "not recorded" : "-"}</span>}
          </span>
        );
      },
    },
    {
      id: "duration",
      header: "Duration",
      align: "end",
      cell: (run) => (
        <span className={`whitespace-nowrap ${dim}`} data-testid="trace-row-duration">{formatDuration(runDurationMs(run))}</span>
      ),
    },
    {
      id: "conversation",
      header: "Conversation / Turn",
      cell: (run) =>
        run.conversation_id && run.turn !== null ? (
          <span className={`flex flex-col gap-0.5 ${dim}`}>
            <span>Conv: {run.conversation_id.slice(0, 8)}...</span>
            <span className="text-caption">Turn: {run.turn}</span>
          </span>
        ) : (
          <span className={dim}>N/A</span>
        ),
    },
    {
      id: "trace",
      header: "Trace",
      cell: (run) => (
        <TraceLink runId={run.run_id} data-testid="trace-row-open" className={linkClass}>
          Open
        </TraceLink>
      ),
    },
    {
      id: "grafana",
      header: "Grafana",
      cell: (run) =>
        run.trace_id ? (
          <span className="inline-flex gap-2">
            <a href={buildTraceLink(run.trace_id)} target="_blank" rel="noopener noreferrer" className={linkClass}>
              Trace
            </a>
            <a href={buildLogsLink(run.trace_id)} target="_blank" rel="noopener noreferrer" className={linkClass}>
              Logs
            </a>
          </span>
        ) : (
          <span className={`cursor-not-allowed ${dim}`} title="No trace recorded">N/A</span>
        ),
    },
  ];

  const columnChoice = useColumnVisibility(TRACE_COLUMNS_STORAGE_KEY, DEFAULT_HIDDEN_COLUMNS);
  // "time" holds the row's open button, so it cannot be hidden.
  const columns = allColumns.filter((column) => column.id === "time" || columnChoice.isVisible(column.id));
  const chooserColumns = allColumns.map((column) => ({
    id: column.id,
    label: typeof column.header === "string" ? column.header : column.id,
    locked: column.id === "time",
  }));

  return (
    <>
      <PageHeader crumbs={[navCrumb("traces", { current: true })]} />
      <TracesTabs active="traces" />
      <TraceSheetHost runIds={visibleIds ?? runs.map((run) => run.run_id)} baseParams={pendingParams}>
        {(sheet) => (
          <div className="flex min-w-0 flex-col gap-4 p-4">
            <p className="text-label text-muted-foreground">Runs with their span tree, tokens and cost</p>
            {showRuns ? <RunsHistogram filters={filters} onPickBucket={({ from, to }) => setFilters({ from, to })}
              onResetRange={() => setFilters({ from: null, to: null })} /> : null}

            <TraceFilterBar
              filters={filters}
              setFilters={setFilters}
              onClear={() => writeFilters(EMPTY_FILTERS)}
              agentNames={agentNames}
              trailing={<ColumnChooser columns={chooserColumns} hidden={columnChoice.hidden} onVisibleChange={columnChoice.setVisible} onReset={columnChoice.reset} />}
            />

            {showRuns && error ? (
              <div role="alert" className="rounded-md border border-destructive/40 bg-destructive/10 p-3 text-sm text-destructive-text">
                {error}
              </div>
            ) : null}

            {!showRuns ? null : loading && runs.length === 0 ? (
              <div className="flex items-center justify-center py-12">
                <p className="text-muted-foreground">Loading runs...</p>
              </div>
            ) : (
              <>
                <div
                  aria-busy={reloading}
                  data-testid="traces-table"
                  className={`min-w-0 transition-opacity motion-reduce:transition-none ${reloading ? "pointer-events-none opacity-50" : ""}`}
                >
                  <DataList<TraceRun>
                    label="Runs"
                    rows={runs}
                    columns={columns}
                    getRowId={(run) => run.run_id}
                    onRowOpen={(run) => sheet.open(run.run_id)}
                    getRowLabel={(run) => `Open the trace of the run started ${new Date(run.created_at).toLocaleString()}`}
                    getRowTestId={() => TRACE_ROW.testId}
                    selectedRowId={sheet.runId}
                    onVisibleRowIdsChange={setVisibleIds}
                    empty={{ title: hasActiveFilters(filters) ? "No runs match these filters." : "No runs yet." }}
                    testId="traces-list"
                  />
                </div>

                {nextBefore ? (
                  <div className="flex justify-center">
                    <Button variant="secondary" size="sm" onClick={() => loadRuns(nextBefore)} disabled={loading}>
                      {loading ? "Loading..." : "Load more"}
                    </Button>
                  </div>
                ) : null}
              </>
            )}

            {showExternal ? (
              <section className="flex min-w-0 flex-col gap-2" aria-label="External traces" data-testid="external-traces">
                <h2 className="text-label font-semibold text-foreground">External traces (OpenTelemetry)</h2>
                {externalError ? <div role="alert" className="rounded-md border border-destructive/40 bg-destructive/10 p-3 text-sm text-destructive-text">{externalError}</div> : null}
                <DataList<ExternalTrace>
                  label="External traces"
                  rows={external}
                  columns={externalColumns}
                  getRowId={(trace) => trace.trace_id}
                  onRowOpen={(trace) => setOpenExternal(trace.trace_id)}
                  getRowLabel={(trace) => `Open the external trace ${trace.name ?? trace.trace_id}`}
                  getRowTestId={() => "external-trace-row"}
                  selectedRowId={openExternal}
                  empty={{ title: "No external traces. Send OpenTelemetry traces with an API key that has the otlp scope (Settings, API keys)." }}
                  testId="external-traces-list"
                />
                {externalNext ? (
                  <div className="flex justify-center">
                    <Button variant="secondary" size="sm" onClick={() => loadExternal(externalNext)}>Load more</Button>
                  </div>
                ) : null}
              </section>
            ) : null}
            {openExternal ? <ExternalTraceSheet traceId={openExternal} onClose={() => setOpenExternal(null)} /> : null}
          </div>
        )}
      </TraceSheetHost>
    </>
  );
}

export default function TracesPage() {
  // useSearchParams needs a Suspense boundary for the static prerender.
  return (
    <Suspense fallback={<><PageHeader crumbs={[navCrumb("traces", { current: true })]} /><p className="py-12 text-center text-muted-foreground">Loading runs...</p></>}>
      <TracesList />
    </Suspense>
  );
}
