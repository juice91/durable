"use client";

import React from "react";
import { BreakdownTable } from "../BreakdownTable";
import { MetricsKpis } from "../MetricsKpis";
import { OperationKpis } from "../OperationKpis";
import { RecentFailures } from "./RecentFailures";
import { breakdownHref } from "../BreakdownTable";
import {
  attentionCount,
  metricsAgentHref,
  recentFailuresHref,
  type BreakdownItem,
  type BreakdownSort,
  type MetricsFilters,
  type MetricsPageState,
  type MetricsSummary,
} from "@/lib/metrics";
import type { TraceRun } from "@/lib/traces";

/** Rows of the Top agents table (5, or 10 with "Show 10") and of the Needs attention list. */
export const TOP_AGENTS_LIMIT = 5;
export const TOP_AGENTS_MORE_LIMIT = 10;
export const RECENT_FAILURES_LIMIT = 5;
/** The Top agents table's sort when the page opens (the former dashboard's choice). */
export const TOP_AGENTS_DEFAULT_SORT: BreakdownSort = "tokens";

interface Section<T> {
  data: T | null;
  loading: boolean;
  error: string | null;
}

export interface MetricsOverviewProps {
  state: MetricsPageState;
  filters: MetricsFilters;
  summary: Section<MetricsSummary>;
  /** The newest failed and needs-attention runs (GET /runs, `recentFailuresFilters(filters)`). */
  failures: Section<readonly TraceRun[]>;
  /** The agent breakdown, top `agentLimit` by `agentSort`. */
  topAgents: Section<readonly BreakdownItem[]>;
  agentSort: BreakdownSort;
  onAgentSortChange: (sort: BreakdownSort) => void;
  /** Rows requested for Top agents: `TOP_AGENTS_LIMIT` or `TOP_AGENTS_MORE_LIMIT` (default the former). */
  agentLimit?: number;
  /** Toggles between 5 and 10 rows; without it there is no toggle. */
  onAgentLimitChange?: (limit: number) => void;
  onNavigate?: (href: string) => void;
  agentNames?: Record<string, string>;
  workflowNames?: Record<string, string>;
}

/**
 * The Metrics page's Overview header (SL.7, the former P0.18 dashboard): the KPI row with period
 * deltas, then Needs attention and Top agents side by side (stacked below 1024 px). Everything here
 * reads the page's one filter state, so the Overview shows exactly the Metrics numbers.
 */
export function MetricsOverview({
  state,
  filters,
  summary,
  failures,
  topAgents,
  agentSort,
  onAgentSortChange,
  agentLimit = TOP_AGENTS_LIMIT,
  onAgentLimitChange,
  onNavigate,
  agentNames,
  workflowNames,
}: MetricsOverviewProps) {
  const expanded = agentLimit > TOP_AGENTS_LIMIT;
  // Offer "Show 10" only when the top 5 may hide more agents (a full page of 5 rows came back).
  const canToggle = Boolean(onAgentLimitChange) && (expanded || (topAgents.data?.length ?? 0) >= TOP_AGENTS_LIMIT);
  return (
    <section aria-labelledby="metrics-overview-title" className="min-w-0 space-y-4" data-testid="metrics-overview">
      <h2 id="metrics-overview-title" className="text-base font-semibold text-foreground">Overview</h2>
      {summary.error ? (
        <p role="alert" className="rounded-md border border-destructive bg-destructive/10 p-3 text-sm text-foreground">
          {summary.error}
        </p>
      ) : (
        <MetricsKpis summary={summary.data} filters={filters} loading={summary.loading} />
      )}
      {/* RM.19: embedding and memory apart from chat, from the same summary (not while narrowed to
          one operation: the main KPIs are that operation then). */}
      {!summary.error && !state.operation ? (
        <OperationKpis summary={summary.data} loading={summary.loading} state={state} />
      ) : null}
      <div className="grid gap-4 lg:grid-cols-2">
        <RecentFailures
          runs={failures.data}
          total={summary.data ? attentionCount(summary.data.current) : null}
          viewAllHref={recentFailuresHref(filters)}
          agentNames={agentNames}
          workflowNames={workflowNames}
          loading={failures.loading}
          error={failures.error}
          headingLevel={3}
        />
        <BreakdownTable
          title="Top agents"
          by="agent"
          items={topAgents.data}
          filters={filters}
          sort={agentSort}
          onSortChange={onAgentSortChange}
          onNavigate={onNavigate}
          loading={topAgents.loading}
          error={topAgents.error}
          emptyMessage="No agent runs in this period"
          note={state.kind && state.kind !== "agent" ? "Tool and workflow runs have no agent; agent steps of workflows are counted through their workflow." : undefined}
          headingLevel={3}
          rowLink={(item, name) =>
            // Already narrowed to this agent: the row's next step is its runs, not the current URL.
            state.agent_id === item.key
              ? { href: breakdownHref(item, "agent", filters), label: `${name}: open matching runs` }
              : { href: metricsAgentHref(state, item.key), label: `${name}: show this agent's metrics` }
          }
          actions={
            canToggle ? (
              <button
                type="button"
                className="rounded-md border border-input bg-background px-2 py-1 text-xs text-foreground hover:bg-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                aria-pressed={expanded}
                onClick={() => onAgentLimitChange!(expanded ? TOP_AGENTS_LIMIT : TOP_AGENTS_MORE_LIMIT)}
                data-testid="top-agents-limit"
              >
                {expanded ? `Show ${TOP_AGENTS_LIMIT}` : `Show ${TOP_AGENTS_MORE_LIMIT}`}
              </button>
            ) : null
          }
        />
      </div>
    </section>
  );
}
