// Metrics API client (P0.6's GET /api/v1/metrics/*) and the pure helpers of the Metrics page and its
// Overview header (the former P0.18 dashboard, folded in by SL.7): filters <-> query strings,
// date-range presets, period deltas, number formatting, chart summaries and the drill-down links to
// the Traces list.
import { ApiError, BASE_URL, apiErrorFromResponse, apiHeaders } from "./api";
import { formatCost } from "./format";
import { routes } from "@/lib/routes";
import type { TraceFilters } from "./traces";

// ------------------------------------------------------------------------------------- types

export type MetricsKind = "agent" | "tool" | "workflow" | "kb_ingest";
export type MetricsInterval = "hour" | "day";
export type BreakdownBy = "agent" | "model" | "operation";

/**
 * What a model request was for (RM.19; the API's `operation`). The first five partition the usage
 * rows: `embedding` (knowledge base ingest, retrieve, the search tester, semantic recall and index),
 * and among chat requests `moderation`, `observer`, `reflector` and `chat` (every other model
 * request). `memory` is a filter only: observer, reflector and the recall and index embeddings.
 */
export type MetricsOperation = "chat" | "moderation" | "observer" | "reflector" | "embedding" | "memory";

/** The operations a `by=operation` breakdown returns (a partition of the requests). */
export const BREAKDOWN_OPERATIONS: readonly Exclude<MetricsOperation, "memory">[] = [
  "chat", "embedding", "observer", "reflector", "moderation",
];

/** Filter order and labels. */
export const OPERATION_LABELS: Record<MetricsOperation, string> = {
  chat: "Chat",
  embedding: "Embedding",
  memory: "Memory (all)",
  observer: "Observer",
  reflector: "Reflector",
  moderation: "Moderation",
};

/** A breakdown row's name for an operation key (unknown keys as they are). */
export function operationLabel(key: string): string {
  return key === "memory" ? "Memory" : (OPERATION_LABELS as Record<string, string>)[key] ?? key;
}
export type BreakdownSort = "runs" | "failed" | "requests" | "tokens" | "cost";

/** Every metric the API returns for a period, a bucket or a breakdown group. */
export interface MetricValues {
  runs: number;
  completed: number;
  failed: number;
  needs_attention: number;
  in_progress: number;
  /** Failed runs blocked by a processor. Null in breakdown items (not computed per group). */
  blocked: number | null;
  /** failed / (completed + failed + needs_attention); null when no run has finished. */
  error_rate: number | null;
  latency_p50_ms: number | null;
  latency_p95_ms: number | null;
  requests: number;
  input_tokens: number;
  output_tokens: number;
  cache_read_tokens: number;
  cache_write_tokens: number;
  /** Exact decimal string (sum of priced requests only). */
  cost_usd: string;
  unpriced_requests: number;
  unmeasured_requests: number;
  /** Summary periods only (RM.19; absent from older APIs): the usage of each operation (they sum
   * to the totals) and of `memory`. */
  operations?: Partial<Record<MetricsOperation, UsageValues>>;
}

/** The usage half of the metrics (per operation in a summary). */
export type UsageValues = Pick<
  MetricValues,
  "requests" | "input_tokens" | "output_tokens" | "cache_read_tokens" | "cache_write_tokens" | "cost_usd" | "unpriced_requests" | "unmeasured_requests"
>;

export interface MetricsFilterEcho {
  agent_id: string | null;
  model: string | null;
  provider: string | null;
  kind: MetricsKind | null;
  /** RM.19 (absent from older APIs). */
  operation?: MetricsOperation | null;
}

export interface MetricsSummary {
  from: string;
  to: string;
  filters: MetricsFilterEcho;
  current: MetricValues;
  previous: MetricValues | null;
  previous_from: string | null;
  previous_to: string | null;
}

export type MetricsBucket = { start: string } & MetricValues;

export interface MetricsTimeseries {
  from: string;
  to: string;
  interval: MetricsInterval;
  filters: MetricsFilterEcho;
  buckets: MetricsBucket[];
}

export type BreakdownItem = { key: string; label: string | null } & MetricValues;

export interface MetricsBreakdown {
  from: string;
  to: string;
  by: BreakdownBy;
  sort: BreakdownSort;
  filters: MetricsFilterEcho;
  items: BreakdownItem[];
}

/** The query every metrics call shares. `from`/`to` are UTC ISO strings. */
export interface MetricsFilters {
  from: string;
  to: string;
  agent_id?: string | null;
  model?: string | null;
  provider?: string | null;
  kind?: MetricsKind | null;
  operation?: MetricsOperation | null;
}

// ----------------------------------------------------------------------------- bounds

export const MAX_RANGE_DAYS = 90;
export const MAX_BUCKETS = 400;
const HOUR_MS = 3_600_000;
const DAY_MS = 24 * HOUR_MS;
export const INTERVAL_MS: Record<MetricsInterval, number> = { hour: HOUR_MS, day: DAY_MS };

// ------------------------------------------------------------------------ query strings

/** The common query (`from`, `to`, `agent_id`, `model`, `provider`, `kind`, `operation`); empty values left out. */
export function metricsQueryParams(filters: MetricsFilters): URLSearchParams {
  const params = new URLSearchParams();
  params.set("from", filters.from);
  params.set("to", filters.to);
  for (const key of ["agent_id", "model", "provider", "kind", "operation"] as const) {
    const value = filters[key];
    if (value) params.set(key, value);
  }
  return params;
}

export function summaryPath(filters: MetricsFilters, compare = true): string {
  const params = metricsQueryParams(filters);
  if (compare) params.set("compare", "previous");
  return `/api/v1/metrics/summary?${params.toString()}`;
}

export function timeseriesPath(
  filters: MetricsFilters,
  interval?: MetricsInterval | null,
  metrics?: readonly (keyof MetricValues)[],
): string {
  const params = metricsQueryParams(filters);
  if (interval) params.set("interval", interval);
  for (const metric of metrics ?? []) params.append("metric", metric);
  return `/api/v1/metrics/timeseries?${params.toString()}`;
}

export function breakdownPath(
  filters: MetricsFilters,
  by: BreakdownBy,
  sort: BreakdownSort = "runs",
  limit = 10,
): string {
  const params = metricsQueryParams(filters);
  params.set("by", by);
  params.set("sort", sort);
  params.set("limit", String(limit));
  return `/api/v1/metrics/breakdown?${params.toString()}`;
}

// ------------------------------------------------------------------------------- fetch

/** A metrics error for display: 422 range messages verbatim, 503 as a timeout hint. */
export function metricsErrorMessage(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.status === 503) return "The metrics query timed out. Narrow the time range or add a filter.";
    if (error.status === 0) return error.message;
    if (error.status === 404) return "The metrics API is not available on this server (the API may need a restart).";
    return error.message || `HTTP ${error.status}`;
  }
  return error instanceof Error ? error.message : "Failed to load metrics";
}

async function getJson<T>(path: string, signal?: AbortSignal): Promise<T> {
  const response = await fetch(`${BASE_URL}${path}`, { headers: apiHeaders(), signal });
  if (!response.ok) throw await apiErrorFromResponse(response);
  return response.json() as Promise<T>;
}

export function fetchMetricsSummary(filters: MetricsFilters, signal?: AbortSignal): Promise<MetricsSummary> {
  return getJson(summaryPath(filters, true), signal);
}

export function fetchMetricsTimeseries(
  filters: MetricsFilters,
  interval?: MetricsInterval | null,
  signal?: AbortSignal,
): Promise<MetricsTimeseries> {
  return getJson(timeseriesPath(filters, interval), signal);
}

export function fetchMetricsBreakdown(
  filters: MetricsFilters,
  by: BreakdownBy,
  sort: BreakdownSort = "runs",
  limit = 10,
  signal?: AbortSignal,
): Promise<MetricsBreakdown> {
  return getJson(breakdownPath(filters, by, sort, limit), signal);
}

// ------------------------------------------------------------------------- date ranges

export type RangePreset = "24h" | "7d" | "30d" | "90d" | "custom";

export const RANGE_PRESETS: { value: Exclude<RangePreset, "custom">; label: string; ms: number }[] = [
  { value: "24h", label: "Last 24 hours", ms: DAY_MS },
  { value: "7d", label: "Last 7 days", ms: 7 * DAY_MS },
  { value: "30d", label: "Last 30 days", ms: 30 * DAY_MS },
  { value: "90d", label: "Last 90 days", ms: 90 * DAY_MS },
];

/** `[from, to)` for a preset, ending at `now` (minute precision so URLs and caches are stable). */
export function presetRange(preset: Exclude<RangePreset, "custom">, now: Date = new Date()): { from: string; to: string } {
  const found = RANGE_PRESETS.find((p) => p.value === preset) ?? RANGE_PRESETS[1];
  const end = Math.ceil(now.getTime() / 60_000) * 60_000;
  return { from: new Date(end - found.ms).toISOString(), to: new Date(end).toISOString() };
}

/** Client-side check of the API's bounds; returns the API's own message or null when valid. */
export function validateRange(from: string, to: string, interval?: MetricsInterval | null): string | null {
  const start = Date.parse(from);
  const end = Date.parse(to);
  if (Number.isNaN(start) || Number.isNaN(end)) return "Enter a valid start and end time";
  if (start >= end) return "from must be before to";
  if (end - start > MAX_RANGE_DAYS * DAY_MS) return `the range must be at most ${MAX_RANGE_DAYS} days`;
  if (interval && bucketCount(from, to, interval) > MAX_BUCKETS) {
    return `at most ${MAX_BUCKETS} ${interval} buckets; choose a shorter range or a longer interval`;
  }
  return null;
}

/** The interval the API picks when none is given: hour up to 2 days, else day. */
export function defaultInterval(from: string, to: string): MetricsInterval {
  return Date.parse(to) - Date.parse(from) <= 2 * DAY_MS ? "hour" : "day";
}

/** Number of UTC buckets the API returns (first bucket starts at `from` truncated). */
export function bucketCount(from: string, to: string, interval: MetricsInterval): number {
  const step = INTERVAL_MS[interval];
  const first = Math.floor(Date.parse(from) / step) * step;
  return Math.max(1, Math.ceil((Date.parse(to) - first) / step));
}

/** The bucket's `[start, end)`, clamped to the queried `[from, to)` so a drill-down matches the metric. */
export function bucketRange(
  bucketStart: string,
  interval: MetricsInterval,
  range: { from: string; to: string },
): { from: string; to: string } {
  const start = Date.parse(bucketStart);
  const end = start + INTERVAL_MS[interval];
  const from = Math.max(start, Date.parse(range.from));
  const to = Math.min(end, Date.parse(range.to));
  return { from: new Date(from).toISOString(), to: new Date(to).toISOString() };
}

// -------------------------------------------------------------------------- page URL

/** The Metrics page's own URL state. Presets keep only `range`; custom ranges keep `from`/`to`. */
export interface MetricsPageState {
  range: RangePreset;
  from: string | null;
  to: string | null;
  interval: MetricsInterval | null;
  agent_id: string | null;
  model: string | null;
  provider: string | null;
  kind: MetricsKind | null;
  /** RM.19; optional so states built before it stay valid (absent = no operation filter). */
  operation?: MetricsOperation | null;
}

export const DEFAULT_PAGE_STATE: MetricsPageState = {
  range: "7d", from: null, to: null, interval: null, agent_id: null, model: null, provider: null, kind: null,
};

type ParamReader = { get(name: string): string | null };

const KINDS = new Set<string>(["agent", "tool", "workflow", "kb_ingest"]);
const OPERATIONS = new Set<string>(Object.keys(OPERATION_LABELS));
const RANGES = new Set<string>(["24h", "7d", "30d", "90d", "custom"]);

export function parsePageState(params: ParamReader | null | undefined): MetricsPageState {
  if (!params) return { ...DEFAULT_PAGE_STATE };
  const one = (name: string) => {
    const v = params.get(name);
    return v && v.trim() !== "" ? v.trim() : null;
  };
  const from = one("from");
  const to = one("to");
  const rawRange = one("range");
  let range: RangePreset = rawRange && RANGES.has(rawRange) ? (rawRange as RangePreset) : "7d";
  // A drill-down style link with explicit from/to and no preset is a custom range.
  if (!rawRange && from && to) range = "custom";
  if (range === "custom" && !(from && to && !Number.isNaN(Date.parse(from)) && !Number.isNaN(Date.parse(to)))) {
    range = "7d";
  }
  const interval = one("interval");
  const kind = one("kind");
  const operation = one("operation");
  return {
    range,
    from: range === "custom" ? from : null,
    to: range === "custom" ? to : null,
    interval: interval === "hour" || interval === "day" ? interval : null,
    agent_id: one("agent_id"),
    model: one("model"),
    provider: one("provider"),
    kind: kind && KINDS.has(kind) ? (kind as MetricsKind) : null,
    // Present only when set, so states from before RM.19 compare equal.
    ...(operation && OPERATIONS.has(operation) ? { operation: operation as MetricsOperation } : {}),
  };
}

export function pageStateToParams(state: MetricsPageState): URLSearchParams {
  const params = new URLSearchParams();
  if (state.range !== "7d") params.set("range", state.range);
  if (state.range === "custom") {
    if (state.from) params.set("from", state.from);
    if (state.to) params.set("to", state.to);
  }
  if (state.interval) params.set("interval", state.interval);
  for (const key of ["agent_id", "model", "provider", "kind", "operation"] as const) {
    const v = state[key];
    if (v) params.set(key, v);
  }
  return params;
}

/** Resolve the page state to the API's filters (presets end at `now`). */
export function pageStateToFilters(state: MetricsPageState, now: Date = new Date()): MetricsFilters {
  const { from, to } =
    state.range === "custom" && state.from && state.to
      ? { from: state.from, to: state.to }
      : presetRange(state.range === "custom" ? "7d" : state.range, now);
  return {
    from, to, agent_id: state.agent_id, model: state.model, provider: state.provider, kind: state.kind,
    ...(state.operation ? { operation: state.operation } : {}),
  };
}

// ----------------------------------------------------------------------- drill-down

/** A link to the Traces list with the same filters (contract: `agent_id`, `model`, `provider`,
 * `status` comma-separated, `from`, `to`, `kind`), optionally narrowed. The Traces list has no
 * `operation` filter, so an operation is never carried over. */
export function tracesHref(
  filters: MetricsFilters,
  overrides: Partial<MetricsFilters> & { status?: string[] } = {},
): string {
  const merged: MetricsFilters = { ...filters, ...overrides };
  const params = new URLSearchParams();
  if (overrides.status?.length) params.set("status", overrides.status.join(","));
  params.set("from", merged.from);
  params.set("to", merged.to);
  for (const key of ["agent_id", "model", "provider", "kind"] as const) {
    const v = merged[key];
    if (v) params.set(key, v);
  }
  return routes.traces(params);
}

/** Statuses a "failed" drill-down shows (the error rate counts `failed`). */
export const FAILED_STATUSES = ["failed"];

// ----------------------------------------------------------------------- deltas

export type DeltaDirection = "up" | "down" | "flat";
/** Whether a rise is good, bad or neither for this metric (cost, latency, errors: bad). */
export type DeltaPolarity = "higher-better" | "lower-better" | "neutral";

export interface PeriodDelta {
  direction: DeltaDirection;
  /** Relative change in percent; null when the previous value is 0 or missing. */
  percent: number | null;
  /** Absolute change (for rates: percentage points). */
  absolute: number;
  /** "better" / "worse" / "neutral", from the polarity. */
  tone: "better" | "worse" | "neutral";
  /** Plain text, e.g. "up 12.5% from 40", "no data in the previous period". */
  text: string;
  /** True when the previous period had no value to compare with. */
  noPrevious?: boolean;
}

export function computeDelta(
  current: number | null,
  previous: number | null | undefined,
  polarity: DeltaPolarity = "neutral",
  format: (v: number) => string = (v) => String(v),
): PeriodDelta | null {
  if (current === null || previous === undefined) return null;
  if (previous === null) {
    return { direction: "flat", percent: null, absolute: 0, tone: "neutral", text: "no data in the previous period", noPrevious: true };
  }
  const absolute = current - previous;
  const direction: DeltaDirection = Math.abs(absolute) < 1e-12 ? "flat" : absolute > 0 ? "up" : "down";
  const percent = previous === 0 ? null : (absolute / Math.abs(previous)) * 100;
  let tone: PeriodDelta["tone"] = "neutral";
  if (direction !== "flat" && polarity !== "neutral") {
    const rising = direction === "up";
    tone = (polarity === "higher-better") === rising ? "better" : "worse";
  }
  let text: string;
  if (direction === "flat") text = `no change from ${format(previous)}`;
  else if (percent === null) text = `${direction} from ${format(previous)}`;
  else text = `${direction} ${Math.abs(percent).toFixed(Math.abs(percent) < 10 ? 1 : 0)}% from ${format(previous)}`;
  return { direction, percent, absolute, tone, text };
}

// ----------------------------------------------------------------------- formatting

/** The cost cell text: "no price" when every request in it was unpriced. */
export function formatCostWithPricing(values: Pick<MetricValues, "cost_usd" | "requests" | "unpriced_requests">): string {
  if (values.requests > 0 && values.unpriced_requests >= values.requests) return "no price";
  return formatCost(values.cost_usd) ?? "—";
}

/** "1,234", "12.3K", "4.5M". */
export function formatCount(value: number | null | undefined): string {
  if (value === null || value === undefined) return "—";
  const abs = Math.abs(value);
  if (abs >= 1e9) return `${trim(value / 1e9)}B`;
  if (abs >= 1e6) return `${trim(value / 1e6)}M`;
  if (abs >= 1e4) return `${trim(value / 1e3)}K`;
  return Math.round(value).toLocaleString("en-US");
}

function trim(n: number): string {
  return n.toFixed(1).replace(/\.0$/, "");
}

/** "850 ms", "1.2 s", "2.5 min". */
export function formatLatency(ms: number | null | undefined): string {
  if (ms === null || ms === undefined) return "—";
  if (ms < 1000) return `${Math.round(ms)} ms`;
  if (ms < 60_000) return `${trim(ms / 1000)} s`;
  return `${trim(ms / 60_000)} min`;
}

/** 0.125 -> "12.5%". */
export function formatPercent(rate: number | null | undefined): string {
  if (rate === null || rate === undefined) return "—";
  const pct = rate * 100;
  return `${pct === 0 || pct >= 10 ? Math.round(pct * 10) / 10 : pct.toFixed(1)}%`;
}

/** Bucket label in UTC (buckets are UTC): "Nov 10" for days, "Nov 10, 14:00" for hours. */
export function formatBucket(start: string, interval: MetricsInterval): string {
  const d = new Date(start);
  const day = d.toLocaleDateString("en-US", { month: "short", day: "numeric", timeZone: "UTC" });
  if (interval === "day") return day;
  const time = d.toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit", timeZone: "UTC" });
  return `${day}, ${time}`;
}

/** "Nov 6, 14:00 – Nov 13, 14:00 UTC". */
export function formatRange(from: string, to: string): string {
  return `${formatBucket(from, "hour")} – ${formatBucket(to, "hour")} UTC`;
}

// ---------------------------------------------------------------------- chart data

/** Sum of a series' non-null values. */
export function seriesTotal(values: readonly (number | null)[]): number {
  return values.reduce<number>((sum, v) => sum + (v ?? 0), 0);
}

/** A one-sentence text summary of a series for screen readers and the chart's caption. */
export function summarizeSeries(
  label: string,
  buckets: readonly { start: string }[],
  values: readonly (number | null)[],
  interval: MetricsInterval,
  format: (v: number) => string,
  aggregate: "sum" | "none" = "sum",
): string {
  const present = values.map((v, i) => ({ v, i })).filter((p): p is { v: number; i: number } => p.v !== null);
  const unit = interval === "hour" ? "hourly" : "daily";
  if (present.length === 0 || present.every((p) => p.v === 0 && aggregate === "sum")) {
    return `${label}: no data in ${buckets.length} ${unit} buckets.`;
  }
  const peak = present.reduce((a, b) => (b.v > a.v ? b : a));
  const low = present.reduce((a, b) => (b.v < a.v ? b : a));
  const head =
    aggregate === "sum"
      ? `${label}: ${format(seriesTotal(values))} in total over ${buckets.length} ${unit} buckets`
      : `${label} over ${buckets.length} ${unit} buckets`;
  return `${head}; highest ${format(peak.v)} at ${formatBucket(buckets[peak.i].start, interval)}, lowest ${format(low.v)} at ${formatBucket(buckets[low.i].start, interval)}.`;
}

/** A "nice" axis maximum (1, 2, 2.5, 5 × 10^n) at or above `max`. */
export function niceMax(max: number): number {
  if (!(max > 0)) return 1;
  const exp = Math.floor(Math.log10(max));
  const base = 10 ** exp;
  for (const m of [1, 2, 2.5, 5, 10]) {
    if (m * base >= max - 1e-12) return m * base;
  }
  return 10 * base;
}

/** Model ids seen in a breakdown -> provider names ("openai:gpt-4o" -> "openai"). */
export function providersOf(models: readonly string[]): string[] {
  return Array.from(new Set(models.map((m) => m.split(":")[0]).filter(Boolean))).sort();
}

// ------------------------------------------------------------------- custom range inputs

// ------------------------------------------------------------------------- filter options

/** An entry of GET /api/v1/models (the platform allowlist plus permitted scripted test models). */
export interface ModelOption {
  id: string;
  provider: string;
  name: string;
  /** `provider:*` allowlist entry: any name of that provider. */
  wildcard: boolean;
  source: "allowlist" | "scripted";
  description: string | null;
}

export async function fetchModels(signal?: AbortSignal): Promise<ModelOption[]> {
  const data = await getJson<{ items: ModelOption[] }>("/api/v1/models", signal);
  return Array.isArray(data?.items) ? data.items : [];
}

/** Model and provider filter options: every listed (non-wildcard) model plus the models seen in
 * metrics, so a model used in the past but no longer allowed stays selectable. Sorted, unique. */
export function modelFilterOptions(
  listed: readonly ModelOption[],
  seen: readonly string[],
): { models: string[]; providers: string[] } {
  const models = Array.from(
    new Set([...listed.filter((m) => !m.wildcard).map((m) => m.id), ...seen].filter(Boolean)),
  ).sort();
  const providers = Array.from(
    new Set([...listed.map((m) => m.provider), ...providersOf(models)].filter(Boolean)),
  ).sort();
  return { models, providers };
}

// ------------------------------------------------------------------- overview (former dashboard)
//
// SL.7 folded the P0.18 Overview Dashboard into the Metrics page: one page, one filter state, one
// set of metrics queries, so the Overview's numbers are the Metrics numbers by construction. The
// helpers below were `lib/dashboard.ts`; their names stay exported for one milestone.

/** The Metrics page narrowed to one agent, keeping every other filter (the Top agents drill-down). */
export function metricsAgentHref(state: MetricsPageState, agentId: string): string {
  return routes.metrics(pageStateToParams({ ...state, agent_id: agentId }));
}

/** The Metrics page narrowed to one operation, keeping every other filter (RM.19 drill-down). */
export function metricsOperationHref(state: MetricsPageState, operation: MetricsOperation): string {
  return routes.metrics(pageStateToParams({ ...state, operation }));
}

/** Whether `key` is an operation the API's filter takes. */
export function isMetricsOperation(key: string): key is MetricsOperation {
  return OPERATIONS.has(key);
}

/** Run statuses the "Needs attention" panel lists: failed runs and runs that need attention. */
export const ATTENTION_STATUSES = ["failed", "needs_attention"] as const;

/** The Traces list filters of the "Needs attention" panel (same range and filters, both statuses). */
export function recentFailuresFilters(filters: MetricsFilters): TraceFilters {
  return {
    status: [...ATTENTION_STATUSES],
    from: filters.from,
    to: filters.to,
    model: filters.model ?? null,
    provider: filters.provider ?? null,
    agent_id: filters.agent_id ?? null,
    kind: filters.kind ?? null,
  };
}

/** "View all" for the panel: the Traces list with the same range, filters and statuses. */
export function recentFailuresHref(filters: MetricsFilters): string {
  return tracesHref(filters, { status: [...ATTENTION_STATUSES] });
}

/** Failed plus needs-attention top-level runs of the period (from the summary, so it matches the KPIs). */
export function attentionCount(values: Pick<MetricValues, "failed" | "needs_attention">): number {
  return values.failed + values.needs_attention;
}

/** @deprecated The dashboard's URL state: the page state restricted to the date range. Kept for one
 * milestone after SL.7; the Metrics page parses its full state with `parsePageState`. */
export function parseDashboardState(params: ParamReader | null | undefined): MetricsPageState {
  const parsed = parsePageState(params);
  return { ...DEFAULT_PAGE_STATE, range: parsed.range, from: parsed.from, to: parsed.to };
}

/** @deprecated The range-only query string (`range=30d`, or `range=custom&from=…&to=…`; empty for 7 days). */
export function dashboardParams(state: MetricsPageState): URLSearchParams {
  return pageStateToParams({ ...DEFAULT_PAGE_STATE, range: state.range, from: state.from, to: state.to });
}

/** @deprecated The Overview now lives on Metrics: the Metrics page for the state's date range. */
export function dashboardPath(state: MetricsPageState): string {
  return routes.metrics(dashboardParams(state));
}

/** @deprecated The Metrics page for the state's date range, optionally narrowed (e.g. to one agent);
 * other filters are dropped. Use `metricsAgentHref` to narrow while keeping the page's filters. */
export function metricsPageHref(
  state: MetricsPageState,
  narrow: Partial<Pick<MetricsPageState, "agent_id" | "model" | "provider" | "kind">> = {},
): string {
  return routes.metrics(pageStateToParams({ ...DEFAULT_PAGE_STATE, range: state.range, from: state.from, to: state.to, ...narrow }));
}
