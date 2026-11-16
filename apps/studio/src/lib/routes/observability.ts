import { seg, withQuery, type RouteQuery } from "./query";
import type { LegacyRule } from "./legacy";

/** The trace Sheet's tabs (SL.12). `spans` is the default and is left out of URLs. */
export const TRACE_TABS = ["spans", "events", "scores", "logs"] as const;
export type TraceTab = (typeof TRACE_TABS)[number];
/** Query parameter holding the open trace Sheet's run id. */
export const TRACE_PARAM = "trace";
/** Query parameter holding the trace Sheet's tab (on `/traces`; a host page may use another name). */
export const TRACE_TAB_PARAM = "tab";
/** Query parameter holding the span selected when the trace Sheet opens (from the Spans list). */
export const TRACE_SPAN_PARAM = "span";

/** A URL's `tab` value as a trace tab; anything else (or nothing) is the default, `spans`. */
export function parseTraceTab(raw: string | null | undefined): TraceTab {
  return (TRACE_TABS as readonly string[]).includes(raw ?? "") ? (raw as TraceTab) : "spans";
}

export interface TraceRouteOptions {
  /** The Sheet tab to open on; `spans` (the default) is left out of the URL. */
  tab?: TraceTab | null;
  /** The span to select when the Sheet opens. */
  span?: string | null;
}

/**
 * Observability pages. Pages that own a filter format (Metrics, Traces, Logs) build their own
 * URLSearchParams and pass them here, so the path lives in one place. The former dashboard at `/`
 * is a legacy redirect since SL.7 (`legacy.ts`); its Overview is the top of Metrics.
 *
 * Since SL.12 a trace is a Sheet over the Traces list: `/traces?trace=<runId>[&tab=events|logs]`.
 * The old `/traces/:runId` and `/runs/:id` pages are redirects (`TRACE_LEGACY_RULES`).
 */
export const observabilityRoutes = {
  /** `/metrics`, with the Metrics page state parameters. */
  metrics: (query?: RouteQuery): string => withQuery("/metrics", query),
  /** `/traces`, with the Traces list filters. */
  traces: (query?: RouteQuery): string => withQuery("/traces", query),
  /** `/traces/spans`, with the Spans list filters. */
  spans: (query?: RouteQuery): string => withQuery("/traces/spans", query),
  /**
   * `/traces?trace=:runId[&tab=]`: one run's trace in the trace Sheet. Pages that host the Sheet
   * open it in place instead (`TraceLink`), with the same URL as the link's href.
   * Throws RouteIdError for "." and "..", like every id builder.
   */
  trace: (runId: string, { tab = null, span = null }: TraceRouteOptions = {}): string => {
    seg(runId);
    const params = new URLSearchParams();
    params.set(TRACE_PARAM, runId);
    if (tab && tab !== "spans") params.set(TRACE_TAB_PARAM, tab);
    if (span) params.set(TRACE_SPAN_PARAM, span);
    return `/traces?${params.toString()}`;
  },
  /** A run's details: the trace Sheet on its Events tab (the former `/runs/:id` page). */
  run: (runId: string): string => observabilityRoutes.trace(runId, { tab: "events" }),
  /** `/threads?agent_id=&q=`: the conversations of every agent (W2.5). A row opens the thread in the agent's chat. */
  threads: (query?: RouteQuery): string => withQuery("/threads", query),
  /** `/logs`, with the Logs filters. */
  logs: (query?: RouteQuery): string => withQuery("/logs", query),
  /** The retired `/traces/:runId` page path (a redirect since SL.12). */
  legacyTracePath: (runId: string): string => `/traces/${seg(runId)}`,
  /** The retired `/runs/:id` page path (a redirect since SL.12). */
  legacyRunPath: (runId: string): string => `/runs/${seg(runId)}`,
};

/**
 * SL.12's legacy rows (the route map): `/traces/:runId` opens the trace Sheet; `/runs/:id` opens it
 * on the Events tab. Other query parameters are not carried (the old pages read none).
 */
export const TRACE_LEGACY_RULES: readonly LegacyRule[] = [
  { task: "SL.12", pattern: "/traces/:runId", to: ({ runId }) => observabilityRoutes.trace(runId) },
  { task: "SL.12", pattern: "/runs/:id", to: ({ id }) => observabilityRoutes.trace(id, { tab: "events" }) },
];
