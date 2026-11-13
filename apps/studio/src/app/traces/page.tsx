"use client";

import React, { Suspense, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { isoToLocalInput, localInputToIso } from "@/lib/datetimeLocal";
import { formatCost, formatTokens } from "@/lib/format";
import { useRouter, useSearchParams } from "next/navigation";
import { StatusPill } from "@/components/runs/StatusPill";
import { buildTraceLink, buildLogsLink } from "@/lib/grafana";
import { api } from "@/lib/api";
import { listWorkflows } from "@/lib/workflows";
import { RunSubject } from "@/components/runs/RunSubject";
import {
  formatDuration,
  hasActiveFilters,
  listTraceRuns,
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
import { TraceLink } from "@/components/traces/TraceLink";
import { TraceSheetHost, withSheetParams } from "@/components/traces/TraceSheetHost";

/** The list row's test id (kept from P0.13), as a `testId` key so the test-id inventory sees it rendered. */
const TRACE_ROW = { testId: "trace-row" } as const;

const TERMINAL: ReadonlySet<string> = new Set(["completed", "failed", "needs_attention"]);

const STATUS_OPTIONS: Array<{ value: string; label: string }> = [
  { value: "completed", label: "Completed" },
  { value: "failed", label: "Failed" },
  { value: "running", label: "Running" },
  { value: "queued", label: "Queued" },
  { value: "recovering", label: "Recovering" },
  { value: "needs_attention", label: "Needs Attention" },
];

const inputClass =
  "h-8 rounded-md border border-input bg-surface px-2 text-label text-foreground placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring";
const labelClass = "text-caption font-medium text-muted-foreground";
const dim = "text-muted-foreground";
const linkClass = "text-info-text underline-offset-4 hover:underline";

function TextFilter({ id, label, value, placeholder, onCommit }: {
  id: string; label: string; value: string | null; placeholder: string; onCommit: (value: string | null) => void;
}) {
  const [draft, setDraft] = useState(value ?? "");
  useEffect(() => setDraft(value ?? ""), [value]);
  const commit = () => {
    const next = draft.trim() || null;
    if (next !== value) onCommit(next);
  };
  return (
    <div className="flex min-w-0 flex-col gap-1">
      <label htmlFor={id} className={labelClass}>{label}</label>
      <input
        id={id}
        value={draft}
        placeholder={placeholder}
        onChange={(e) => setDraft(e.target.value)}
        onBlur={commit}
        onKeyDown={(e) => {
          if (e.key === "Enter") commit();
        }}
        className={`${inputClass} w-full sm:w-44`}
      />
    </div>
  );
}

function TracesList() {
  const router = useRouter();
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

  // The rows' ids as the list shows them, for the trace Sheet's prev / next.
  const [visibleIds, setVisibleIds] = useState<string[] | null>(null);

  // The latest URL, so a filter written while the trace Sheet opens (a text filter committing on
  // blur as focus moves into the Sheet) keeps `trace` and `tab` instead of closing it.
  const latestParams = useRef(searchParams);
  latestParams.current = searchParams;
  const setFilters = useCallback((patch: Partial<TraceFilters>) => {
    router.replace(routes.traces(withSheetParams(traceFiltersToParams({ ...filters, ...patch }), latestParams.current)));
  }, [filters, router]);

  // Every request (first page or "Load more") takes a new generation; a response is applied only
  // while its generation is still the latest, so a slow page for old filters (or an old cursor)
  // never overwrites or extends newer results.
  const requestRef = useRef(0);

  const loadRuns = useCallback(async (before?: string) => {
    const request = ++requestRef.current;
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
  }, [filters]);

  useEffect(() => {
    void loadRuns();
  }, [loadRuns]);

  const statusValue = filters.status.length === 0 ? "all" : filters.status.length === 1 ? filters.status[0] : filters.status.join(",");
  const customStatus = filters.status.length > 1 || (filters.status.length === 1 && !STATUS_OPTIONS.some((o) => o.value === filters.status[0]));
  const agentOptions = Object.entries(agentNames).sort((a, b) => a[1].localeCompare(b[1]));

  const columns: DataListColumn<TraceRun>[] = [
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
          <RunSubject run={run} agentNames={agentNames} workflowNames={workflowNames} />
        </span>
      ),
    },
    { id: "status", header: "Status", cell: (run) => <StatusPill status={run.display_status} /> },
    {
      id: "model",
      header: "Model",
      cell: (run) => (
        <span className="font-mono text-caption" data-testid="trace-row-model">
          {run.model_id ?? <span className={dim}>-</span>}
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

  return (
    <>
      <PageHeader crumbs={[navCrumb("traces", { current: true })]} />
      <TraceSheetHost runIds={visibleIds ?? runs.map((run) => run.run_id)}>
        {(sheet) => (
          <div className="flex min-w-0 flex-col gap-4 p-4">
            <p className="text-label text-muted-foreground">Runs with their span tree, tokens and cost</p>

            <div className="flex min-w-0 flex-wrap items-end gap-3 rounded-lg border border-border bg-surface p-3" role="search" aria-label="Trace filters" data-testid="traces-filter-bar">
              <div className="flex min-w-0 flex-col gap-1">
                <label htmlFor="status-filter" className={labelClass}>
                  Filter by status:
                </label>
                <select
                  id="status-filter"
                  value={statusValue}
                  onChange={(e) => setFilters({ status: e.target.value === "all" ? [] : e.target.value.split(",") })}
                  className={inputClass}
                >
                  <option value="all">All</option>
                  {STATUS_OPTIONS.map((o) => (
                    <option key={o.value} value={o.value}>{o.label}</option>
                  ))}
                  {customStatus ? <option value={statusValue}>{filters.status.join(", ").replace(/_/g, " ")}</option> : null}
                </select>
              </div>
              <div className="flex min-w-0 flex-col gap-1">
                <label htmlFor="kind-filter" className={labelClass}>Kind</label>
                <select
                  id="kind-filter"
                  value={filters.kind ?? "all"}
                  onChange={(e) => setFilters({ kind: e.target.value === "all" ? null : (e.target.value as TraceFilters["kind"]) })}
                  className={inputClass}
                >
                  <option value="all">All</option>
                  <option value="agent">Agent</option>
                  <option value="workflow">Workflow</option>
                  <option value="tool">Tool</option>
                </select>
              </div>
              <div className="flex min-w-0 flex-col gap-1">
                <label htmlFor="agent-filter" className={labelClass}>Agent</label>
                <select
                  id="agent-filter"
                  value={filters.agent_id ?? ""}
                  onChange={(e) => setFilters({ agent_id: e.target.value || null })}
                  className={`${inputClass} max-w-48`}
                >
                  <option value="">All agents</option>
                  {agentOptions.map(([id, name]) => (
                    <option key={id} value={id}>{name}</option>
                  ))}
                  {filters.agent_id && !agentNames[filters.agent_id] ? (
                    <option value={filters.agent_id}>{filters.agent_id.slice(0, 8)}...</option>
                  ) : null}
                </select>
              </div>
              <TextFilter id="model-filter" label="Model" value={filters.model} placeholder="provider:name" onCommit={(model) => setFilters({ model })} />
              <TextFilter id="provider-filter" label="Provider" value={filters.provider} placeholder="e.g. openai" onCommit={(provider) => setFilters({ provider })} />
              <div className="flex min-w-0 flex-col gap-1">
                <label htmlFor="from-filter" className={labelClass}>From</label>
                <input
                  id="from-filter"
                  type="datetime-local"
                  value={isoToLocalInput(filters.from)}
                  onChange={(e) => setFilters({ from: localInputToIso(e.target.value) })}
                  className={inputClass}
                />
              </div>
              <div className="flex min-w-0 flex-col gap-1">
                <label htmlFor="to-filter" className={labelClass}>To</label>
                <input
                  id="to-filter"
                  type="datetime-local"
                  value={isoToLocalInput(filters.to)}
                  onChange={(e) => setFilters({ to: localInputToIso(e.target.value) })}
                  className={inputClass}
                />
              </div>
              {hasActiveFilters(filters) ? (
                <Button variant="ghost" size="sm" onClick={() => router.replace(routes.traces(withSheetParams(new URLSearchParams(), latestParams.current)))}>
                  Clear filters
                </Button>
              ) : null}
            </div>

            {error ? (
              <div role="alert" className="rounded-md border border-destructive/40 bg-destructive/10 p-3 text-sm text-destructive-text">
                {error}
              </div>
            ) : null}

            {loading && runs.length === 0 ? (
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
