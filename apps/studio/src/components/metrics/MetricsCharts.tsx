"use client";

import React from "react";
import { formatTokens, formatUsd, parseCost } from "@/lib/format";
import { TimeSeriesChart, type ChartSeries } from "./TimeSeriesChart";
import {
  FAILED_STATUSES,
  bucketRange,
  formatCount,
  formatLatency,
  summarizeSeries,
  tracesHref,
  type MetricsFilters,
  type MetricsTimeseries,
} from "@/lib/metrics";

export type ChartId = "runs" | "latency" | "tokens" | "cost";
export const ALL_CHARTS: ChartId[] = ["runs", "latency", "tokens", "cost"];

export interface ChartSpec {
  id: ChartId;
  title: string;
  description: string;
  type: "bar" | "line";
  series: ChartSeries[];
  formatValue: (v: number) => string;
  bucketHref: (i: number) => string;
  summary: string;
  extraTooltip?: (i: number) => string[];
  integer?: boolean;
  /** What the drill-down opens (usage charts bucket by request time; runs are listed by creation time). */
  drillLabel?: string;
}

/** Usage (tokens, cost) is bucketed by model-request time, but GET /runs filters by run creation time,
 * so a usage drill-down lists the runs created in the bucket, which may differ slightly. */
export const USAGE_DRILL_LABEL = "Open runs created in this window";
const USAGE_NOTE = "Counted by request time";

/** Map a timeseries response to chart specs (pure; tested directly). */
export function chartSpecs(ts: MetricsTimeseries, filters: MetricsFilters, ids: readonly ChartId[] = ALL_CHARTS): ChartSpec[] {
  const b = ts.buckets;
  const range = { from: ts.from, to: ts.to };
  const bucketFilters = (i: number) => ({ ...filters, ...bucketRange(b[i].start, ts.interval, range) });
  const bucketHref = (i: number) => tracesHref(bucketFilters(i));
  const failedHref = (i: number) => tracesHref(bucketFilters(i), { status: FAILED_STATUSES });
  const summarize = (label: string, values: (number | null)[], fmt: (v: number) => string, agg: "sum" | "none" = "sum") =>
    summarizeSeries(label, b, values, ts.interval, fmt, agg);

  const runs = b.map((x) => x.runs);
  const failed = b.map((x) => x.failed);
  const p50 = b.map((x) => x.latency_p50_ms);
  const p95 = b.map((x) => x.latency_p95_ms);
  const input = b.map((x) => x.input_tokens);
  const output = b.map((x) => x.output_tokens);
  const cost = b.map((x) => parseCost(x.cost_usd) ?? 0);

  const specs: Record<ChartId, ChartSpec> = {
    runs: {
      id: "runs",
      title: "Runs and failures",
      description: "Top-level runs started per bucket; select a bar to open those runs",
      type: "bar",
      series: [
        { key: "runs", label: "Runs", color: "var(--viz-series-1-color)", values: runs, href: bucketHref },
        { key: "failed", label: "Failed", color: "var(--viz-critical-color)", values: failed, href: failedHref },
      ],
      formatValue: formatCount,
      integer: true,
      bucketHref,
      summary: `${summarize("Runs", runs, formatCount)} ${summarize("Failed", failed, formatCount)}`,
    },
    latency: {
      id: "latency",
      title: "Latency",
      description: "p50 and p95 of finished runs, by start time",
      type: "line",
      series: [
        { key: "p50", label: "p50", color: "var(--viz-series-1-color)", values: p50 },
        { key: "p95", label: "p95", color: "var(--viz-series-2-color)", values: p95 },
      ],
      formatValue: formatLatency,
      bucketHref,
      summary: `${summarize("Latency p50", p50, formatLatency, "none")} ${summarize("Latency p95", p95, formatLatency, "none")}`,
    },
    tokens: {
      id: "tokens",
      title: "Tokens",
      description: "Input and output tokens of model requests",
      type: "bar",
      series: [
        { key: "input", label: "Input", color: "var(--viz-series-1-color)", values: input },
        { key: "output", label: "Output", color: "var(--viz-series-2-color)", values: output },
      ],
      formatValue: formatTokens,
      integer: true,
      bucketHref,
      summary: `${summarize("Input tokens", input, formatTokens)} ${summarize("Output tokens", output, formatTokens)}`,
      drillLabel: USAGE_DRILL_LABEL,
      extraTooltip: (i) => [
        ...(b[i].unmeasured_requests ? [`${b[i].unmeasured_requests} requests not measured`] : []),
        USAGE_NOTE,
      ],
    },
    cost: {
      id: "cost",
      title: "Cost (USD)",
      description: "Priced model requests; requests without a price are counted separately",
      type: "bar",
      series: [{ key: "cost", label: "Cost", color: "var(--viz-series-1-color)", values: cost }],
      formatValue: formatUsd,
      bucketHref,
      summary: summarize("Cost", cost, formatUsd),
      drillLabel: USAGE_DRILL_LABEL,
      extraTooltip: (i) => [
        ...(b[i].unpriced_requests ? [`${b[i].unpriced_requests} requests have no price`] : []),
        USAGE_NOTE,
      ],
    },
  };
  return ids.map((id) => specs[id]);
}

export interface MetricsChartsProps {
  timeseries: MetricsTimeseries | null;
  filters: MetricsFilters;
  ids?: readonly ChartId[];
  onNavigate?: (href: string) => void;
  loading?: boolean;
  error?: string | null;
  className?: string;
}

export function MetricsCharts({ timeseries, filters, ids = ALL_CHARTS, onNavigate, loading = false, error = null, className = "" }: MetricsChartsProps) {
  const specs = timeseries ? chartSpecs(timeseries, filters, ids) : null;
  return (
    <div className={`grid gap-4 lg:grid-cols-2 ${className}`}>
      {ids.map((id, n) => {
        const spec = specs?.[n];
        return (
          <TimeSeriesChart
            key={id}
            title={spec?.title ?? PLACEHOLDER_TITLES[id]}
            description={spec?.description}
            buckets={timeseries?.buckets ?? []}
            interval={timeseries?.interval ?? "day"}
            series={spec?.series ?? []}
            type={spec?.type ?? "bar"}
            formatValue={spec?.formatValue ?? String}
            bucketHref={spec?.bucketHref}
            extraTooltip={spec?.extraTooltip}
            integer={spec?.integer}
            drillLabel={spec?.drillLabel}
            summary={spec?.summary}
            onNavigate={onNavigate}
            loading={loading}
            error={error}
            className="min-w-0"
          />
        );
      })}
    </div>
  );
}

const PLACEHOLDER_TITLES: Record<ChartId, string> = {
  runs: "Runs and failures",
  latency: "Latency",
  tokens: "Tokens",
  cost: "Cost (USD)",
};
