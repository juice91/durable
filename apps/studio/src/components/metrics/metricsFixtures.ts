// Test fixtures shaped like the metrics API responses (P0.6).
import type { BreakdownItem, MetricValues, MetricsBreakdown, MetricsSummary, MetricsTimeseries } from "@/lib/metrics";

export function metricValues(overrides: Partial<MetricValues> = {}): MetricValues {
  return {
    runs: 0, completed: 0, skipped: 0, failed: 0, needs_attention: 0, in_progress: 0, blocked: 0, error_rate: null,
    latency_p50_ms: null, latency_p95_ms: null, latency_p99_ms: null, requests: 0, input_tokens: 0, output_tokens: 0,
    cache_read_tokens: 0, cache_write_tokens: 0, cost_usd: "0E-10", unpriced_requests: 0, unmeasured_requests: 0,
    ...overrides,
  };
}

export const RANGE = { from: "2026-11-10T00:00:00+00:00", to: "2026-11-13T00:00:00+00:00" };
const NO_FILTERS = { agent_id: null, model: null, provider: null, kind: null };

export function summaryFixture(current: Partial<MetricValues> = {}, previous: Partial<MetricValues> | null = {}): MetricsSummary {
  return {
    ...RANGE,
    filters: NO_FILTERS,
    current: metricValues(current),
    previous: previous === null ? null : metricValues(previous),
    previous_from: previous === null ? null : "2026-11-07T00:00:00+00:00",
    previous_to: previous === null ? null : RANGE.from,
  };
}

export function timeseriesFixture(buckets: Partial<MetricValues>[] = []): MetricsTimeseries {
  const days = ["2026-11-10", "2026-11-11", "2026-11-12"];
  return {
    ...RANGE,
    interval: "day",
    filters: NO_FILTERS,
    buckets: days.map((d, i) => ({ start: `${d}T00:00:00+00:00`, ...metricValues(buckets[i] ?? {}) })),
  };
}

export function breakdownFixture(by: "agent" | "model", items: (Partial<BreakdownItem> & { key: string })[]): MetricsBreakdown {
  return {
    ...RANGE,
    by,
    sort: "runs",
    filters: NO_FILTERS,
    // The API returns `blocked: null` for breakdown items.
    items: items.map((i) => ({ label: by === "model" ? i.key : null, ...metricValues(i), blocked: null, ...i })),
  };
}
