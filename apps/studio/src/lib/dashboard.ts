// Overview Dashboard (P0.18) helpers. The dashboard shows the Metrics page's numbers for a date range:
// it keeps the Metrics page's URL state (`range`, or `from`/`to` for a custom range) and resolves it
// with the same `pageStateToFilters`, so for the same URL state both pages send the same metrics
// queries and render the same values. Every drill-down goes to Metrics (same range) or Traces.
import {
  DEFAULT_PAGE_STATE,
  pageStateToParams,
  parsePageState,
  tracesHref,
  type MetricValues,
  type MetricsFilters,
  type MetricsPageState,
} from "./metrics";
import type { TraceFilters } from "./traces";
import { routes } from "@/lib/routes";

type ParamReader = { get(name: string): string | null };

/** The dashboard's URL state: the Metrics page state restricted to the date range (the dashboard has
 * no dimension filters and lets the API pick the interval, exactly like the Metrics page default). */
export function parseDashboardState(params: ParamReader | null | undefined): MetricsPageState {
  const parsed = parsePageState(params);
  return { ...DEFAULT_PAGE_STATE, range: parsed.range, from: parsed.from, to: parsed.to };
}

/** The dashboard's own query string (`range=30d`, or `range=custom&from=…&to=…`; empty for 7 days). */
export function dashboardParams(state: MetricsPageState): URLSearchParams {
  return pageStateToParams({ ...DEFAULT_PAGE_STATE, range: state.range, from: state.from, to: state.to });
}

export function dashboardPath(state: MetricsPageState): string {
  return routes.dashboard(dashboardParams(state));
}

/** The Metrics page for the dashboard's range, optionally narrowed (e.g. to one agent). Presets stay
 * presets, so the Metrics page resolves the same range the dashboard shows. */
export function metricsPageHref(
  state: MetricsPageState,
  narrow: Partial<Pick<MetricsPageState, "agent_id" | "model" | "provider" | "kind">> = {},
): string {
  const params = pageStateToParams({
    ...DEFAULT_PAGE_STATE,
    range: state.range,
    from: state.from,
    to: state.to,
    ...narrow,
  });
  return routes.metrics(params);
}

/** Run statuses the "Recent failures" panel lists: failed runs and runs that need attention. */
export const ATTENTION_STATUSES = ["failed", "needs_attention"] as const;

/** The Traces list filters of the "Recent failures" panel (same range, both statuses). */
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

/** "View all" for the panel: the Traces list with the same range and statuses. */
export function recentFailuresHref(filters: MetricsFilters): string {
  return tracesHref(filters, { status: [...ATTENTION_STATUSES] });
}

/** Failed plus needs-attention top-level runs of the period (from the summary, so it matches Metrics). */
export function attentionCount(values: Pick<MetricValues, "failed" | "needs_attention">): number {
  return values.failed + values.needs_attention;
}
