"use client";

import React, { Suspense, useCallback, useEffect, useMemo, useState } from "react";
import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { api } from "@/lib/api";
import { listWorkflows } from "@/lib/workflows";
import { BreakdownTable, MetricsCharts, MetricsFilterBar, useAutoRefresh, useMetricsQuery } from "@/components/metrics";
import {
  DASHBOARD_CHARTS,
  DashboardKpis,
  RECENT_FAILURES_LIMIT,
  RecentFailures,
  TOP_AGENTS_LIMIT,
} from "@/components/dashboard";
import {
  fetchMetricsBreakdown,
  fetchMetricsSummary,
  fetchMetricsTimeseries,
  formatRange,
  metricsQueryParams,
  pageStateToFilters,
  validateRange,
  type BreakdownSort,
  type MetricsPageState,
} from "@/lib/metrics";
import { listTraceRuns } from "@/lib/traces";
import {
  attentionCount,
  dashboardPath,
  metricsPageHref,
  parseDashboardState,
  recentFailuresFilters,
  recentFailuresHref,
} from "@/lib/dashboard";

/** Preset ranges move forward this often (and re-fetch) while the page is open (as on Metrics). */
const AUTO_REFRESH_MS = 5 * 60_000;

export default function DashboardPage() {
  return (
    <Suspense fallback={<div className="max-w-7xl mx-auto px-4 py-8 text-muted-foreground">Loading dashboard…</div>}>
      <DashboardView />
    </Suspense>
  );
}

const linkCls =
  "rounded-sm text-sm text-primary hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring";

function DashboardView() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const search = searchParams?.toString() ?? "";
  const state = useMemo(() => parseDashboardState(new URLSearchParams(search)), [search]);

  // Same "now" handling as the Metrics page: presets end now, recomputed on URL change, Refresh and
  // every AUTO_REFRESH_MS while the tab is visible (once more on return); a custom range is fixed.
  const [refreshes, setRefreshes] = useState(0);
  const tick = useAutoRefresh(state.range !== "custom", AUTO_REFRESH_MS);
  // eslint-disable-next-line react-hooks/exhaustive-deps
  const now = useMemo(() => new Date(), [search, refreshes, tick]);
  const filters = useMemo(() => pageStateToFilters(state, now), [state, now]);
  const rangeError = validateRange(filters.from, filters.to, state.interval);
  const baseKey = rangeError ? null : `${metricsQueryParams(filters).toString()}#${refreshes}`;

  const [agentSort, setAgentSort] = useState<BreakdownSort>("tokens");

  const summary = useMetricsQuery(baseKey, (signal) => fetchMetricsSummary(filters, signal));
  const timeseries = useMetricsQuery(
    baseKey === null ? null : `${baseKey}|auto`,
    (signal) => fetchMetricsTimeseries(filters, state.interval, signal),
  );
  const topAgents = useMetricsQuery(
    baseKey === null ? null : `${baseKey}|${agentSort}`,
    (signal) => fetchMetricsBreakdown(filters, "agent", agentSort, TOP_AGENTS_LIMIT, signal),
  );
  const failures = useMetricsQuery(
    baseKey === null ? null : `${baseKey}|failures`,
    (signal) => listTraceRuns(recentFailuresFilters(filters), { limit: RECENT_FAILURES_LIMIT, signal }),
  );

  // Names for the recent failures list (best effort; ids are shown when a lookup fails).
  const [agentNames, setAgentNames] = useState<Record<string, string>>({});
  const [workflowNames, setWorkflowNames] = useState<Record<string, string>>({});
  useEffect(() => {
    let cancelled = false;
    Promise.resolve()
      .then(() => api.agents.list())
      .then((list) => {
        if (!cancelled && Array.isArray(list)) setAgentNames(Object.fromEntries(list.map((a) => [a.id, a.name])));
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

  const update = useCallback(
    (next: MetricsPageState) => router.replace(dashboardPath(next), { scroll: false }),
    [router],
  );
  const navigate = useCallback((href: string) => router.push(href), [router]);

  const metricsHref = metricsPageHref(state);
  const nothingYet =
    summary.data !== null && summary.data.current.runs === 0 && summary.data.current.requests === 0;

  return (
    <div className="max-w-7xl mx-auto px-4 py-8 space-y-6">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h1 className="text-2xl font-bold text-card-foreground">Dashboard</h1>
          <p className="text-muted-foreground mt-1">
            Overview for{" "}
            <span className="text-foreground" data-testid="dashboard-range">{formatRange(filters.from, filters.to)}</span>.
            Select a card, bar, agent or run for the details.
          </p>
        </div>
        <Link href={metricsHref} className={linkCls} data-testid="dashboard-open-metrics">
          Open in Metrics
        </Link>
      </div>

      <MetricsFilterBar
        state={state}
        onChange={update}
        range={filters}
        dimensions={false}
        showInterval={false}
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
            <DashboardKpis summary={summary.data} filters={filters} state={state} loading={summary.loading} />
          )}

          {nothingYet && (
            <div className="rounded-md border border-border bg-muted/50 p-3 text-sm text-muted-foreground" data-testid="dashboard-empty">
              <p>No runs or model requests in this period. Widen the date range, or start a run:</p>
              <p className="mt-2 flex flex-wrap gap-4">
                <Link href="/agents" className={linkCls}>Agents</Link>
                <Link href="/playground" className={linkCls}>Playground</Link>
                <Link href="/workflows" className={linkCls}>Workflows</Link>
              </p>
            </div>
          )}

          <section aria-labelledby="dashboard-trends" className="space-y-3">
            <div className="flex flex-wrap items-center justify-between gap-2">
              <h2 id="dashboard-trends" className="text-lg font-semibold text-foreground">Trends</h2>
              <Link href={metricsHref} className={linkCls} data-testid="dashboard-trends-metrics">
                Latency, tokens and more in Metrics
              </Link>
            </div>
            <MetricsCharts
              timeseries={timeseries.data}
              filters={filters}
              ids={DASHBOARD_CHARTS}
              onNavigate={navigate}
              loading={timeseries.loading}
              error={timeseries.error}
            />
          </section>

          <div className="grid gap-4 lg:grid-cols-2">
            <BreakdownTable
              title="Top agents"
              by="agent"
              items={topAgents.data?.items ?? null}
              filters={filters}
              sort={agentSort}
              onSortChange={setAgentSort}
              onNavigate={navigate}
              loading={topAgents.loading}
              error={topAgents.error}
              emptyMessage="No agent runs in this period"
              rowLink={(item, name) => ({
                href: metricsPageHref(state, { agent_id: item.key }),
                label: `${name}: open in Metrics`,
              })}
            />
            <RecentFailures
              runs={failures.data?.items ?? null}
              total={summary.data ? attentionCount(summary.data.current) : null}
              viewAllHref={recentFailuresHref(filters)}
              agentNames={agentNames}
              workflowNames={workflowNames}
              loading={failures.loading}
              error={failures.error}
            />
          </div>
        </>
      )}
    </div>
  );
}
