"use client";

import React from "react";
import { KpiCard, comparisonCaption, kpiCards, type KpiId } from "@/components/metrics";
import { metricsPageHref } from "@/lib/dashboard";
import type { MetricsFilters, MetricsPageState, MetricsSummary } from "@/lib/metrics";

/** The dashboard's KPI cards, in order (a subset of the Metrics page's cards). */
export const DASHBOARD_KPIS: readonly KpiId[] = ["runs", "error_rate", "latency_p95", "tokens", "cost"];

const LABELS: Record<KpiId, string> = {
  runs: "Runs",
  error_rate: "Error rate",
  latency_p50: "Latency p50",
  latency_p95: "Latency p95",
  tokens: "Tokens",
  cost: "Cost (USD)",
};

/** The Metrics page's KPI cards (`kpiCards`, same values and deltas) for the dashboard's ids. Cards
 * with a Traces drill-down keep it (runs, errors); the others open the Metrics page for this range. */
export function dashboardKpiCards(summary: MetricsSummary, filters: MetricsFilters, state: MetricsPageState) {
  const metrics = metricsPageHref(state);
  return kpiCards(summary, filters, DASHBOARD_KPIS).map((card) => ({ ...card, href: card.href ?? metrics }));
}

export interface DashboardKpisProps {
  summary: MetricsSummary | null;
  filters: MetricsFilters;
  state: MetricsPageState;
  loading?: boolean;
}

export function DashboardKpis({ summary, filters, state, loading = false }: DashboardKpisProps) {
  const grid = "grid gap-3 grid-cols-2 md:grid-cols-3 xl:grid-cols-5";
  if (!summary) {
    return (
      <div className={grid} aria-busy={loading || undefined}>
        {DASHBOARD_KPIS.map((id) => (
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
        {dashboardKpiCards(summary, filters, state).map(({ id, ...card }) => (
          <KpiCard key={id} {...card} testId={`kpi-${id}`} />
        ))}
      </div>
    </div>
  );
}
