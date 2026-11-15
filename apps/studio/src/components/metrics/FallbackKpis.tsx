"use client";

import React from "react";
import { computeDelta, formatCount, formatPercent, type FallbackValues, type MetricsSummary } from "@/lib/metrics";
import { KpiCard, type KpiCardProps } from "./KpiCard";
import { KPI_GRID } from "./MetricsKpis";

// AC.17: the Overview's model fallback row, beside the embedding and memory KPIs: how often an
// agent's primary model failed and a fallback answered, from the page's one summary (`fallback`).

export type FallbackKpiId = "fallback_rate" | "fallback_failed_attempts" | "fallback_exhausted";

export const FALLBACK_KPI_LABELS: Record<FallbackKpiId, string> = {
  fallback_rate: "Not answered by the primary model",
  fallback_failed_attempts: "Billed failed attempts",
  fallback_exhausted: "Runs with every model failed",
};

type CardSpec = Omit<KpiCardProps, "loading"> & { id: FallbackKpiId };

function fallbackOf(summary: MetricsSummary | null, period: "current" | "previous"): FallbackValues | null {
  return summary?.[period]?.fallback ?? null;
}

function active(v: FallbackValues | null): boolean {
  return !!v && (v.fell_back > 0 || v.exhausted_runs > 0 || v.billed_failed_attempts > 0);
}

/** The cards from the summary's `fallback` block (pure). None from an API before AC.17, or when
 * neither period saw a fallback answer, an exhausted chain or a billed failed attempt (a tenant
 * without fallback models sees no 0% row). */
export function fallbackCards(summary: MetricsSummary | null): CardSpec[] {
  const cur = fallbackOf(summary, "current");
  const prev = summary?.previous ? fallbackOf(summary, "previous") : null;
  if (!cur || (!active(cur) && !active(prev))) return [];
  const deltaLabel = "vs previous period";
  const rateDelta =
    prev && cur.fallback_rate !== null && prev.fallback_rate !== null
      ? computeDelta(cur.fallback_rate, prev.fallback_rate, "lower-better", formatPercent)
      : null;
  const missed = cur.fell_back + cur.exhausted_runs;
  return [
    {
      id: "fallback_rate",
      label: FALLBACK_KPI_LABELS.fallback_rate,
      value: formatPercent(cur.fallback_rate),
      delta: rateDelta,
      hint: `${formatCount(missed)} of ${formatCount(cur.chat_requests + cur.exhausted_runs)} agent chat requests: answered by a fallback model or failed on every model`,
      deltaLabel,
    },
    {
      id: "fallback_failed_attempts",
      label: FALLBACK_KPI_LABELS.fallback_failed_attempts,
      value: formatCount(cur.billed_failed_attempts),
      delta: prev ? computeDelta(cur.billed_failed_attempts, prev.billed_failed_attempts, "lower-better", formatCount) : null,
      hint: "Failed attempts the provider billed; an attempt that raised before answering is not billed",
      deltaLabel,
    },
    {
      id: "fallback_exhausted",
      label: FALLBACK_KPI_LABELS.fallback_exhausted,
      value: formatCount(cur.exhausted_runs),
      delta: prev ? computeDelta(cur.exhausted_runs, prev.exhausted_runs, "lower-better", formatCount) : null,
      hint: "Runs that failed because every model of the chain failed",
      deltaLabel,
    },
  ];
}

export interface FallbackKpisProps {
  summary: MetricsSummary | null;
  loading?: boolean;
}

/** "Model fallback": a titled KPI row, shown once the summary reports fallback activity. */
export function FallbackKpis({ summary, loading = false }: FallbackKpisProps) {
  const cards = fallbackCards(summary);
  if (cards.length === 0) return null;
  return (
    <section aria-labelledby="metrics-fallback-kpis-title" className="min-w-0 space-y-2" data-testid="metrics-fallback-kpis">
      <h3 id="metrics-fallback-kpis-title" className="text-sm font-semibold text-foreground">Model fallback</h3>
      <div className={KPI_GRID} aria-busy={loading || undefined}>
        {cards.map(({ id, ...card }) => (
          <KpiCard key={id} {...card} testId={`kpi-${id}`} />
        ))}
      </div>
    </section>
  );
}
