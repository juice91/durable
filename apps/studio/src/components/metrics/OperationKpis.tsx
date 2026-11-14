"use client";

import React from "react";
import { formatTokens } from "@/lib/format";
import {
  computeDelta,
  formatCostWithPricing,
  formatCount,
  metricsOperationHref,
  type MetricsPageState,
  type MetricsSummary,
  type UsageValues,
} from "@/lib/metrics";
import { KpiCard, type KpiCardProps } from "./KpiCard";
import { KPI_GRID, costDelta } from "./MetricsKpis";

// RM.19: the Overview's second KPI row, embedding and memory usage kept apart from chat. The cards
// read the page's one summary (its `operations` split: the same range, filters and request as the
// main KPIs), compare with the previous period, and narrow the Metrics page to that operation.

export type OperationKpiId = "embedding_tokens" | "embedding_cost" | "memory_tokens" | "memory_cost";

export const OPERATION_KPIS: readonly OperationKpiId[] = ["embedding_tokens", "embedding_cost", "memory_tokens", "memory_cost"];

export const OPERATION_KPI_LABELS: Record<OperationKpiId, string> = {
  embedding_tokens: "Embedding tokens",
  embedding_cost: "Embedding cost (USD)",
  memory_tokens: "Memory tokens",
  memory_cost: "Memory cost (USD)",
};

type CardSpec = Omit<KpiCardProps, "loading"> & { id: OperationKpiId };

const tokens = (v: UsageValues) => v.input_tokens + v.output_tokens;

function costHint(v: UsageValues, what: string): string {
  return v.unpriced_requests > 0
    ? `${formatCount(v.unpriced_requests)} of ${formatCount(v.requests)} ${what} have no price and are not included`
    : `${formatCount(v.requests)} ${what}`;
}

/** The summary's `operations` entry for one operation (null from an API before RM.19). */
function usageOf(summary: MetricsSummary | null, period: "current" | "previous", op: "embedding" | "memory"): UsageValues | null {
  return summary?.[period]?.operations?.[op] ?? null;
}

/** The four cards from the summary's `operations` split (pure; none when the API has no split). */
export function operationCards(summary: MetricsSummary | null, state: MetricsPageState): CardSpec[] {
  const cards: CardSpec[] = [];
  const deltaLabel = "vs previous period";
  const embedding = usageOf(summary, "current", "embedding");
  const memory = usageOf(summary, "current", "memory");
  if (embedding) {
    const cur = embedding;
    const prev = summary?.previous ? usageOf(summary, "previous", "embedding") : null;
    const href = metricsOperationHref(state, "embedding");
    cards.push(
      {
        id: "embedding_tokens",
        label: OPERATION_KPI_LABELS.embedding_tokens,
        value: formatTokens(tokens(cur)),
        delta: prev ? computeDelta(tokens(cur), tokens(prev), "neutral", formatTokens) : null,
        hint: `${formatCount(cur.requests)} embedding calls: ingest, retrieval, recall and index`,
        href,
        deltaLabel,
      },
      {
        id: "embedding_cost",
        label: OPERATION_KPI_LABELS.embedding_cost,
        value: formatCostWithPricing(cur),
        delta: costDelta(cur, prev),
        hint: costHint(cur, "embedding calls"),
        href,
        deltaLabel,
      },
    );
  }
  if (memory) {
    const cur = memory;
    const prev = summary?.previous ? usageOf(summary, "previous", "memory") : null;
    const href = metricsOperationHref(state, "memory");
    cards.push(
      {
        id: "memory_tokens",
        label: OPERATION_KPI_LABELS.memory_tokens,
        value: formatTokens(tokens(cur)),
        delta: prev ? computeDelta(tokens(cur), tokens(prev), "neutral", formatTokens) : null,
        hint: `${formatTokens(cur.input_tokens)} in · ${formatTokens(cur.output_tokens)} out: observer, reflector, recall and index`,
        href,
        deltaLabel,
      },
      {
        id: "memory_cost",
        label: OPERATION_KPI_LABELS.memory_cost,
        value: formatCostWithPricing(cur),
        delta: costDelta(cur, prev),
        hint: costHint(cur, "memory requests"),
        href,
        deltaLabel,
      },
    );
  }
  return cards;
}

export interface OperationKpisProps {
  summary: MetricsSummary | null;
  loading?: boolean;
  state: MetricsPageState;
}

/** "Embedding and memory": a titled KPI row under the Overview's main KPIs, shown once the summary
 * has its `operations` split (never while loading, nor from an API before RM.19). */
export function OperationKpis({ summary, loading = false, state }: OperationKpisProps) {
  const cards = operationCards(summary, state);
  if (cards.length === 0) return null;
  return (
    <section aria-labelledby="metrics-operation-kpis-title" className="min-w-0 space-y-2" data-testid="metrics-operation-kpis">
      <h3 id="metrics-operation-kpis-title" className="text-sm font-semibold text-foreground">Embedding and memory</h3>
      <div className={KPI_GRID} aria-busy={loading || undefined}>
        {cards.map(({ id, ...card }) => (
          <KpiCard key={id} {...card} testId={`kpi-${id}`} />
        ))}
      </div>
    </section>
  );
}
