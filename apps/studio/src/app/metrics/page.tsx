"use client";

import React, { Suspense, useCallback, useEffect, useMemo, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { api } from "@/lib/api";
import {
  BreakdownTable,
  MetricsCharts,
  MetricsFilterBar,
  MetricsKpis,
  useAutoRefresh,
  useMetricsQuery,
} from "@/components/metrics";
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

export default function MetricsPage() {
  return (
    <Suspense fallback={<><PageHeader crumbs={[navCrumb("metrics", { current: true })]} /><div className="max-w-7xl mx-auto px-4 py-8 text-muted-foreground">Loading metrics…</div></>}>
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

  const [agentSort, setAgentSort] = useState<BreakdownSort>("runs");
  const [modelSort, setModelSort] = useState<BreakdownSort>("requests");

  const summary = useMetricsQuery(baseKey, (signal) => fetchMetricsSummary(filters, signal));
  const timeseries = useMetricsQuery(
    baseKey === null ? null : `${baseKey}|${state.interval ?? "auto"}`,
    (signal) => fetchMetricsTimeseries(filters, state.interval, signal),
  );
  const byAgent = useMetricsQuery(
    baseKey === null ? null : `${baseKey}|${agentSort}`,
    (signal) => fetchMetricsBreakdown(filters, "agent", agentSort, 10, signal),
  );
  const byModel = useMetricsQuery(
    baseKey === null ? null : `${baseKey}|${modelSort}`,
    (signal) => fetchMetricsBreakdown(filters, "model", modelSort, 10, signal),
  );

  // Filter options: agents of the tenant; models seen in any model breakdown so far.
  const [agents, setAgents] = useState<{ id: string; name: string }[]>([]);
  useEffect(() => {
    let cancelled = false;
    Promise.resolve()
      .then(() => api.agents.list())
      .then((list) => {
        if (!cancelled && Array.isArray(list)) setAgents(list.map((a) => ({ id: a.id, name: a.name })));
      })
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, []);
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

  return (
    <>
      <PageHeader crumbs={[navCrumb("metrics", { current: true })]} />
    <div className="max-w-7xl mx-auto px-4 py-8 space-y-6">
      <div>
        <p className="text-muted-foreground mt-1">
          Runs, errors, latency, tokens and cost for{" "}
          <span className="text-foreground" data-testid="metrics-range">{formatRange(filters.from, filters.to)}</span>.
          Select a bar, point, card or row to open the matching runs.
        </p>
      </div>

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
          {summary.error ? (
            <p role="alert" className="rounded-md border border-destructive bg-destructive/10 p-3 text-sm text-foreground">
              {summary.error}
            </p>
          ) : (
            <MetricsKpis summary={summary.data} filters={filters} loading={summary.loading} />
          )}

          {nothingYet && (
            <p className="rounded-md border border-border bg-muted/50 p-3 text-sm text-muted-foreground" data-testid="metrics-empty">
              No runs or model requests match these filters in this period. Widen the date range or clear the filters.
            </p>
          )}

          <MetricsCharts
            timeseries={timeseries.data}
            filters={filters}
            onNavigate={navigate}
            loading={timeseries.loading}
            error={timeseries.error}
          />

          <div className="grid gap-4">
            <BreakdownTable
              title="By agent"
              by="agent"
              items={byAgent.data?.items ?? null}
              filters={filters}
              sort={agentSort}
              onSortChange={setAgentSort}
              onNavigate={navigate}
              loading={byAgent.loading}
              error={byAgent.error}
              note={state.kind && state.kind !== "agent" ? "Tool and workflow runs have no agent; agent steps of workflows are counted through their workflow." : undefined}
            />
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
          </div>
        </>
      )}
    </div>
    </>
  );
}
