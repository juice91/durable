import { seg, withQuery, type RouteQuery } from "./query";

/**
 * Observability pages. Pages that own a filter format (Metrics, Traces, Logs, the dashboard) build
 * their own URLSearchParams and pass them here, so the path lives in one place.
 */
export const observabilityRoutes = {
  /** `/` (the dashboard today), with its range parameters. */
  dashboard: (query?: RouteQuery): string => withQuery("/", query),
  /** `/metrics`, with the Metrics page state parameters. */
  metrics: (query?: RouteQuery): string => withQuery("/metrics", query),
  /** `/traces`, with the Traces list filters. */
  traces: (query?: RouteQuery): string => withQuery("/traces", query),
  /** `/traces/:runId`: one trace. */
  trace: (runId: string): string => `/traces/${seg(runId)}`,
  /** `/runs/:id`: run details. */
  run: (runId: string): string => `/runs/${seg(runId)}`,
  /** `/logs`, with the Logs filters. */
  logs: (query?: RouteQuery): string => withQuery("/logs", query),
};
