"use client";

import React, { Suspense, useCallback, useEffect, useMemo, useState } from "react";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { api } from "@/lib/api";
import { listWorkflows } from "@/lib/workflows";
import { listTraceRuns } from "@/lib/traces";
import {
  BreakdownTable,
  MetricsCharts,
  MetricsFilterBar,
  useAutoRefresh,
  useMetricsQuery,
} from "@/components/metrics";
import {
  MetricsOverview,
  RECENT_FAILURES_LIMIT,
  TOP_AGENTS_DEFAULT_SORT,
  TOP_AGENTS_LIMIT,
} from "@/components/metrics/overview";
import {
  fetchMetricsBreakdown,
  fetchMetricsSummary,
  fetchMetricsTimeseries,
  formatRange,
  metricsQueryParams,
  pageStateToFilters,
  pageStateToParams,
  parsePageState,
  fetchModels,
  modelFilterOptions,
  recentFailuresFilters,
  validateRange,
  type BreakdownSort,
  type MetricsPageState,
  type ModelOption,
} from "@/lib/metrics";
import { routes } from "@/lib/routes";
import { PageHeader } from "@/components/frame/PageHeader";
import { navCrumb } from "@/lib/nav";

/** Preset ranges move forward this often (and re-fetch) while the page is open. */
const AUTO_REFRESH_MS = 5 * 60_000;

const linkCls =
  "rounded-sm text-primary hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring";
const sectionTitle = "text-base font-semibold text-foreground";

export default function MetricsPage() {
  return (
    <Suspense fallback={<><PageHeader crumbs={[navCrumb("metrics", { current: true })]} /><div className="mx-auto max-w-7xl px-4 py-8 text-muted-foreground">Loading metrics…</div></>}>
      <MetricsView />
    </Suspense>
  );
}

function MetricsView() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const search = searchParams?.toString() ?? "";
  const state = useMemo(() => parsePageState(new URLSearchParams(search)), [search]);

  // Presets end "now": recomputed when the URL state changes (range, filters), on Refresh, and every
  // AUTO_REFRESH_MS while a preset range is shown and the tab is visible, once more when it becomes
  // visible again (useAutoRefresh); a custom range is fixed and is not re-fetched.
  const [refreshes, setRefreshes] = useState(0);
  const tick = useAutoRefresh(state.range !== "custom", AUTO_REFRESH_MS);
  // eslint-disable-next-line react-hooks/exhaustive-deps
  const now = useMemo(() => new Date(), [search, refreshes, tick]);
  const filters = useMemo(() => pageStateToFilters(state, now), [state, now]);
  const rangeError = validateRange(filters.from, filters.to, state.interval);
  const baseKey = rangeError ? null : `${metricsQueryParams(filters).toString()}#${refreshes}`;

  const [agentSort, setAgentSort] = useState<BreakdownSort>(TOP_AGENTS_DEFAULT_SORT);
  const [agentLimit, setAgentLimit] = useState(TOP_AGENTS_LIMIT);
  const [modelSort, setModelSort] = useState<BreakdownSort>("requests");

  // Every query below shares `baseKey` (filters, resolved range, Refresh count), so the Overview,
  // the charts and the breakdowns always show the same window and re-fetch together.
  const summary = useMetricsQuery(baseKey, (signal) => fetchMetricsSummary(filters, signal));
  const timeseries = useMetricsQuery(
    baseKey === null ? null : `${baseKey}|${state.interval ?? "auto"}`,
    (signal) => fetchMetricsTimeseries(filters, state.interval, signal),
  );
  const topAgents = useMetricsQuery(
    baseKey === null ? null : `${baseKey}|${agentSort}|${agentLimit}`,
    (signal) => fetchMetricsBreakdown(filters, "agent", agentSort, agentLimit, signal),
  );
  const byModel = useMetricsQuery(
    baseKey === null ? null : `${baseKey}|${modelSort}`,
    (signal) => fetchMetricsBreakdown(filters, "model", modelSort, 10, signal),
  );
  const failures = useMetricsQuery(
    baseKey === null ? null : `${baseKey}|failures`,
    (signal) => listTraceRuns(recentFailuresFilters(filters), { limit: RECENT_FAILURES_LIMIT, signal }),
  );

  // Filter options and Needs attention row names: the tenant's agents and workflows (best effort;
  // ids are shown when a lookup fails). Models: GET /models plus those seen in a breakdown so far.
  const [agents, setAgents] = useState<{ id: string; name: string }[]>([]);
  const [workflowNames, setWorkflowNames] = useState<Record<string, string>>({});
  useEffect(() => {
    let cancelled = false;
    Promise.resolve()
      .then(() => api.agents.list())
      .then((list) => {
        if (!cancelled && Array.isArray(list)) setAgents(list.map((a) => ({ id: a.id, name: a.name })));
      })
      .catch(() => undefined);
    Promise.resolve()
      .then(() => listWorkflows())
      .then((list) => {
        if (!cancelled && Array.isArray(list)) setWorkflowNames(Object.fromEntries(list.map((w) => [w.id, w.name])));
      })
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, []);
  const agentNames = useMemo(() => Object.fromEntries(agents.map((a) => [a.id, a.name])), [agents]);
  const [listedModels, setListedModels] = useState<ModelOption[]>([]);
  useEffect(() => {
    const controller = new AbortController();
    fetchModels(controller.signal)
      .then((items) => {
        if (!controller.signal.aborted) setListedModels(items);
      })
      .catch(() => undefined);
    return () => controller.abort();
  }, []);
  const [seenModels, setSeenModels] = useState<string[]>([]);
  useEffect(() => {
    const keys = byModel.data?.items.map((i) => i.key) ?? [];
    if (keys.length) setSeenModels((prev) => Array.from(new Set([...prev, ...keys])).sort());
  }, [byModel.data]);

  const modelOptions = useMemo(() => modelFilterOptions(listedModels, seenModels), [listedModels, seenModels]);

  const update = useCallback(
    (next: MetricsPageState) => {
      router.replace(routes.metrics(pageStateToParams(next)), { scroll: false });
    },
    [router],
  );
  const navigate = useCallback((href: string) => router.push(href), [router]);

  const nothingYet =
    summary.data !== null && summary.data.current.runs === 0 && summary.data.current.requests === 0;
  const filtered = Boolean(state.agent_id || state.model || state.provider || state.kind);

  return (
    <>
      <PageHeader crumbs={[navCrumb("metrics", { current: true })]} />
      <div className="mx-auto min-w-0 max-w-7xl space-y-6 px-4 py-8">
        <p className="text-muted-foreground">
          Runs, errors, latency, tokens and cost for{" "}
          <span className="text-foreground" data-testid="metrics-range">{formatRange(filters.from, filters.to)}</span>.
          Select a card, bar, agent or row to open the matching runs.
        </p>

        <MetricsFilterBar
          state={state}
          onChange={update}
          range={filters}
          agents={agents}
          models={modelOptions.models}
          providers={modelOptions.providers}
          onRefresh={() => setRefreshes((n) => n + 1)}
        />

        {rangeError ? (
          <p role="alert" className="rounded-md border border-destructive bg-destructive/10 p-3 text-sm text-foreground">
            {rangeError}
          </p>
        ) : (
          <>
            <MetricsOverview
              state={state}
              filters={filters}
              summary={summary}
              failures={{ ...failures, data: failures.data?.items ?? null }}
              topAgents={{ ...topAgents, data: topAgents.data?.items ?? null }}
              agentSort={agentSort}
              onAgentSortChange={setAgentSort}
              agentLimit={agentLimit}
              onAgentLimitChange={setAgentLimit}
              onNavigate={navigate}
              agentNames={agentNames}
              workflowNames={workflowNames}
            />

            {nothingYet && (
              <div className="rounded-md border border-border bg-muted/50 p-3 text-sm text-muted-foreground" data-testid="metrics-empty">
                {filtered ? (
                  <p>No runs or model requests match these filters in this period. Widen the date range or clear the filters.</p>
                ) : (
                  <>
                    <p>No runs or model requests in this period. Widen the date range, or start a run:</p>
                    <p className="mt-2 flex flex-wrap gap-4">
                      <Link href={routes.agents()} className={linkCls}>Agents</Link>
                      <Link href={routes.workflows()} className={linkCls}>Workflows</Link>
                    </p>
                  </>
                )}
              </div>
            )}

            <section aria-labelledby="metrics-trends-title" className="min-w-0 space-y-3">
              <h2 id="metrics-trends-title" className={sectionTitle}>Trends</h2>
              <MetricsCharts
                timeseries={timeseries.data}
                filters={filters}
                onNavigate={navigate}
                loading={timeseries.loading}
                error={timeseries.error}
              />
            </section>

            <BreakdownTable
              title="By model"
              by="model"
              items={byModel.data?.items ?? null}
              filters={filters}
              sort={modelSort}
              onSortChange={setModelSort}
              onNavigate={navigate}
              loading={byModel.loading}
              error={byModel.error}
            />
          </>
        )}
      </div>
    </>
  );
}
