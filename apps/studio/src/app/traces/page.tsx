"use client";

import React, { Suspense, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { StatusPill } from "@/components/runs/StatusPill";
import { buildTraceLink, buildLogsLink } from "@/lib/grafana";
import { api } from "@/lib/api";
import { listWorkflows } from "@/lib/workflows";
import { RunSubject } from "@/components/runs/RunSubject";
import {
  formatCost,
  formatDuration,
  formatTokens,
  hasActiveFilters,
  isoToLocalInput,
  listTraceRuns,
  localInputToIso,
  parseTraceFilters,
  runDurationMs,
  traceFiltersToParams,
  tracePath,
  type TraceFilters,
  type TraceRun,
} from "@/lib/traces";

const STATUS_OPTIONS: Array<{ value: string; label: string }> = [
  { value: "completed", label: "Completed" },
  { value: "failed", label: "Failed" },
  { value: "running", label: "Running" },
  { value: "queued", label: "Queued" },
  { value: "recovering", label: "Recovering" },
  { value: "needs_attention", label: "Needs Attention" },
];

const inputClass =
  "rounded-md border border-input bg-background px-3 py-2 text-sm ring-offset-background placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2";

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
    <div className="flex flex-col gap-1">
      <label htmlFor={id} className="text-xs font-medium text-muted-foreground">{label}</label>
      <input
        id={id}
        value={draft}
        placeholder={placeholder}
        onChange={(e) => setDraft(e.target.value)}
        onBlur={commit}
        onKeyDown={(e) => {
          if (e.key === "Enter") commit();
        }}
        className={`${inputClass} w-44`}
      />
    </div>
  );
}

function TracesList() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const filterKey = searchParams?.toString() ?? "";
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

  const setFilters = useCallback((patch: Partial<TraceFilters>) => {
    const query = traceFiltersToParams({ ...filters, ...patch }).toString();
    router.replace(query ? `/traces?${query}` : "/traces");
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

  return (
    <div className="max-w-7xl mx-auto px-4 py-8">
      <div className="mb-6">
        <h1 className="text-2xl font-bold text-card-foreground">Traces</h1>
        <p className="text-muted-foreground mt-1">Runs with their span tree, tokens and cost</p>
      </div>

      <div className="mb-4 flex flex-wrap items-end gap-3" role="search" aria-label="Trace filters">
        <div className="flex flex-col gap-1">
          <label htmlFor="status-filter" className="text-xs font-medium text-muted-foreground">
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
        <div className="flex flex-col gap-1">
          <label htmlFor="kind-filter" className="text-xs font-medium text-muted-foreground">Kind</label>
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
        <div className="flex flex-col gap-1">
          <label htmlFor="agent-filter" className="text-xs font-medium text-muted-foreground">Agent</label>
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
        <div className="flex flex-col gap-1">
          <label htmlFor="from-filter" className="text-xs font-medium text-muted-foreground">From</label>
          <input
            id="from-filter"
            type="datetime-local"
            value={isoToLocalInput(filters.from)}
            onChange={(e) => setFilters({ from: localInputToIso(e.target.value) })}
            className={inputClass}
          />
        </div>
        <div className="flex flex-col gap-1">
          <label htmlFor="to-filter" className="text-xs font-medium text-muted-foreground">To</label>
          <input
            id="to-filter"
            type="datetime-local"
            value={isoToLocalInput(filters.to)}
            onChange={(e) => setFilters({ to: localInputToIso(e.target.value) })}
            className={inputClass}
          />
        </div>
        {hasActiveFilters(filters) ? (
          <button
            type="button"
            onClick={() => router.replace("/traces")}
            className="rounded-md px-3 py-2 text-sm text-primary underline-offset-4 hover:underline"
          >
            Clear filters
          </button>
        ) : null}
      </div>

      {error ? (
        <div role="alert" className="mb-4 rounded-md border border-destructive/40 bg-destructive/10 p-3 text-sm text-card-foreground">
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
            className={`overflow-x-auto rounded-md border bg-card text-card-foreground shadow transition-opacity ${reloading ? "pointer-events-none opacity-50" : ""}`}
          >
            <table className="w-full text-sm">
              <thead className="bg-muted/50">
                <tr>
                  <th className="px-4 py-3 text-left font-medium text-muted-foreground">Time</th>
                  <th className="px-4 py-3 text-left font-medium text-muted-foreground">Agent / Tool / Workflow</th>
                  <th className="px-4 py-3 text-left font-medium text-muted-foreground">Status</th>
                  <th className="px-4 py-3 text-left font-medium text-muted-foreground">Model</th>
                  <th className="px-4 py-3 text-right font-medium text-muted-foreground">Tokens (in / out)</th>
                  <th className="px-4 py-3 text-right font-medium text-muted-foreground">Cost</th>
                  <th className="px-4 py-3 text-right font-medium text-muted-foreground">Duration</th>
                  <th className="px-4 py-3 text-left font-medium text-muted-foreground">Conversation / Turn</th>
                  <th className="px-4 py-3 text-left font-medium text-muted-foreground">Trace</th>
                  <th className="px-4 py-3 text-left font-medium text-muted-foreground">Grafana</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-border">
                {runs.length === 0 ? (
                  <tr>
                    <td colSpan={10} className="px-4 py-8 text-center text-muted-foreground">
                      {hasActiveFilters(filters) ? "No runs match these filters." : "No runs yet."}
                    </td>
                  </tr>
                ) : null}
                {runs.map((run) => {
                  const duration = runDurationMs(run);
                  const hasTrace = !!run.trace_id;
                  const hasUsage = run.input_tokens != null || run.output_tokens != null;
                  const terminal = ["completed", "failed", "needs_attention"].includes(run.display_status);
                  const cost = formatCost(run.cost_usd);

                  return (
                    <tr key={run.run_id} className="hover:bg-muted/50 transition-colors" data-testid="trace-row">
                      <td className="px-4 py-3 font-mono text-xs">
                        {new Date(run.created_at).toLocaleString()}
                      </td>
                      <td className="px-4 py-3 font-medium">
                        <RunSubject run={run} agentNames={agentNames} workflowNames={workflowNames} />
                      </td>
                      <td className="px-4 py-3">
                        <StatusPill status={run.display_status} />
                      </td>
                      <td className="px-4 py-3 font-mono text-xs" data-testid="trace-row-model">
                        {run.model_id ?? <span className="text-muted-foreground/60">-</span>}
                        {run.model_overridden ? <span className="ml-1 text-muted-foreground" title="Model overridden for this run">(override)</span> : null}
                      </td>
                      <td className="px-4 py-3 text-right font-mono text-xs" data-testid="trace-row-tokens">
                        {hasUsage
                          ? `${formatTokens(run.input_tokens)} / ${formatTokens(run.output_tokens)}`
                          : <span className="text-muted-foreground/60">{terminal ? "not recorded" : "-"}</span>}
                      </td>
                      <td className="px-4 py-3 text-right font-mono text-xs" data-testid="trace-row-cost">
                        {cost ?? <span className="text-muted-foreground/60">{hasUsage ? "no price" : terminal ? "not recorded" : "-"}</span>}
                      </td>
                      <td className="px-4 py-3 text-right text-muted-foreground" data-testid="trace-row-duration">
                        {formatDuration(duration)}
                      </td>
                      <td className="px-4 py-3 text-muted-foreground">
                        {run.conversation_id && run.turn !== null ? (
                          <div className="flex flex-col gap-0.5">
                            <span>Conv: {run.conversation_id.slice(0, 8)}...</span>
                            <span className="text-xs">Turn: {run.turn}</span>
                          </div>
                        ) : (
                          <span className="text-muted-foreground/60">N/A</span>
                        )}
                      </td>
                      <td className="px-4 py-3">
                        <a
                          href={tracePath(run.run_id)}
                          data-testid="trace-row-open"
                          className="inline-flex items-center text-primary hover:underline"
                        >
                          Open
                        </a>
                      </td>
                      <td className="px-4 py-3">
                        {hasTrace ? (
                          <span className="inline-flex gap-2">
                            <a
                              href={buildTraceLink(run.trace_id!)}
                              target="_blank"
                              rel="noopener noreferrer"
                              className="inline-flex items-center text-primary hover:underline"
                            >
                              Trace
                            </a>
                            <a
                              href={buildLogsLink(run.trace_id!)}
                              target="_blank"
                              rel="noopener noreferrer"
                              className="inline-flex items-center text-primary hover:underline"
                            >
                              Logs
                            </a>
                          </span>
                        ) : (
                          <span
                            className="text-muted-foreground/50 cursor-not-allowed"
                            title="No trace recorded"
                          >
                            N/A
                          </span>
                        )}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>

          {nextBefore && (
            <div className="mt-4 flex justify-center">
              <button
                onClick={() => loadRuns(nextBefore)}
                disabled={loading}
                className="inline-flex items-center justify-center rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground hover:bg-primary/90 disabled:opacity-50 disabled:cursor-not-allowed"
              >
                {loading ? "Loading..." : "Load more"}
              </button>
            </div>
          )}
        </>
      )}
    </div>
  );
}

export default function TracesPage() {
  // useSearchParams needs a Suspense boundary for the static prerender.
  return (
    <Suspense fallback={<p className="py-12 text-center text-muted-foreground">Loading runs...</p>}>
      <TracesList />
    </Suspense>
  );
}
