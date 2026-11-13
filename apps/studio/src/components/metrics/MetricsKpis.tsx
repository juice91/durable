"use client";

import React from "react";
import { KpiCard, type KpiCardProps } from "./KpiCard";
import {
  FAILED_STATUSES,
  computeDelta,
  formatCost,
  formatCostWithPricing,
  formatCount,
  formatLatency,
  formatPercent,
  formatRange,
  parseCost,
  tracesHref,
  type MetricValues,
  type MetricsFilters,
  type MetricsSummary,
  type PeriodDelta,
} from "@/lib/metrics";

export type KpiId = "runs" | "error_rate" | "latency_p50" | "latency_p95" | "tokens" | "cost";

export const ALL_KPIS: KpiId[] = ["runs", "error_rate", "latency_p50", "latency_p95", "tokens", "cost"];

type CardSpec = Omit<KpiCardProps, "loading"> & { id: KpiId };

/** The KPI cards for a summary (pure; the Overview Dashboard reuses it with a subset of ids). */
export function kpiCards(summary: MetricsSummary, filters: MetricsFilters, ids: readonly KpiId[] = ALL_KPIS): CardSpec[] {
  const cur = summary.current;
  const prev: MetricValues | null = summary.previous;
  const has = (p: MetricValues | null) => p !== null;
  const finished = cur.completed + cur.failed + cur.needs_attention;
  const tokens = (v: MetricValues) => v.input_tokens + v.output_tokens;
  const deltaLabel = "vs previous period";

  const cards: Record<KpiId, CardSpec> = {
    runs: {
      id: "runs",
      label: "Runs",
      value: formatCount(cur.runs),
      delta: has(prev) ? computeDelta(cur.runs, prev!.runs, "neutral", formatCount) : null,
      hint: `${formatCount(cur.completed)} completed · ${formatCount(cur.in_progress)} in progress`,
      href: tracesHref(filters),
    },
    error_rate: {
      id: "error_rate",
      label: "Error rate",
      value: formatPercent(cur.error_rate),
      delta: has(prev) ? computeDelta(cur.error_rate, prev!.error_rate, "lower-better", formatPercent) : null,
      hint:
        finished === 0
          ? "No finished runs"
          : `${formatCount(cur.failed)} failed of ${formatCount(finished)} finished${cur.blocked ? ` · ${formatCount(cur.blocked)} blocked` : ""}${cur.needs_attention ? ` · ${formatCount(cur.needs_attention)} need attention` : ""}`,
      href: tracesHref(filters, { status: FAILED_STATUSES }),
    },
    latency_p50: {
      id: "latency_p50",
      label: "Latency p50",
      value: formatLatency(cur.latency_p50_ms),
      delta: has(prev) ? computeDelta(cur.latency_p50_ms, prev!.latency_p50_ms, "lower-better", formatLatency) : null,
      hint: "Median, finished runs",
    },
    latency_p95: {
      id: "latency_p95",
      label: "Latency p95",
      value: formatLatency(cur.latency_p95_ms),
      delta: has(prev) ? computeDelta(cur.latency_p95_ms, prev!.latency_p95_ms, "lower-better", formatLatency) : null,
      hint: "95th percentile, finished runs",
    },
    tokens: {
      id: "tokens",
      label: "Tokens",
      value: formatCount(tokens(cur)),
      delta: has(prev) ? computeDelta(tokens(cur), tokens(prev!), "neutral", formatCount) : null,
      hint: `${formatCount(cur.input_tokens)} in · ${formatCount(cur.output_tokens)} out${cur.unmeasured_requests ? ` · ${formatCount(cur.unmeasured_requests)} requests not measured` : ""}`,
    },
    cost: {
      id: "cost",
      label: "Cost (USD)",
      value: formatCostWithPricing(cur),
      delta: costDelta(cur, prev),
      hint:
        cur.unpriced_requests > 0
          ? `${formatCount(cur.unpriced_requests)} of ${formatCount(cur.requests)} requests have no price and are not included`
          : `${formatCount(cur.requests)} model requests`,
    },
  };
  return ids.map((id) => ({ ...cards[id], deltaLabel }));
}

export interface MetricsKpisProps {
  summary: MetricsSummary | null;
  filters: MetricsFilters;
  ids?: readonly KpiId[];
  loading?: boolean;
  className?: string;
}

/** The cost change; "no previous priced data" when the previous period had no priced request
 * (a "$0.00" baseline would be misleading), none when nothing is priced now. */
export function costDelta(cur: MetricValues, prev: MetricValues | null): PeriodDelta | null {
  if (!prev || cur.requests <= cur.unpriced_requests) return null;
  if (prev.requests <= prev.unpriced_requests) {
    return { direction: "flat", percent: null, absolute: 0, tone: "neutral", text: "no previous priced data", noPrevious: true };
  }
  return computeDelta(parseCost(cur.cost_usd), parseCost(prev.cost_usd), "lower-better", formatCost);
}

/** "Compared with Nov 6, 00:00 – Nov 13, 00:00 UTC", or null without a comparison. */
export function comparisonCaption(summary: MetricsSummary | null): string | null {
  if (!summary?.previous || !summary.previous_from || !summary.previous_to) return null;
  return `Changes compare with the previous period, ${formatRange(summary.previous_from, summary.previous_to)}.`;
}

/** A responsive row of KPI cards with deltas against the previous period. */
export function MetricsKpis({ summary, filters, ids = ALL_KPIS, loading = false, className = "" }: MetricsKpisProps) {
  const grid = `grid gap-3 grid-cols-2 md:grid-cols-3 xl:grid-cols-6 ${className}`;
  if (!summary) {
    return (
      <div className={grid} aria-busy={loading || undefined}>
        {ids.map((id) => (
          <KpiCard key={id} label={LABELS[id]} value="—" loading={loading} testId={`kpi-${id}`} />
        ))}
      </div>
    );
  }
  const caption = comparisonCaption(summary);
  return (
    <div className="space-y-2">
      {caption && <p className="text-xs text-muted-foreground" data-testid="kpi-comparison">{caption}</p>}
      <div className={grid} aria-busy={loading || undefined}>
        {kpiCards(summary, filters, ids).map(({ id, ...card }) => (
          <KpiCard key={id} {...card} testId={`kpi-${id}`} />
        ))}
      </div>
    </div>
  );
}

const LABELS: Record<KpiId, string> = {
  runs: "Runs",
  error_rate: "Error rate",
  latency_p50: "Latency p50",
  latency_p95: "Latency p95",
  tokens: "Tokens",
  cost: "Cost (USD)",
};
