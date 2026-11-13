import { seg, withQuery, type RouteQuery } from "./query";

/**
 * Observability pages. Pages that own a filter format (Metrics, Traces, Logs) build their own
 * URLSearchParams and pass them here, so the path lives in one place. The former dashboard at `/`
 * is a legacy redirect since SL.7 (`legacy.ts`); its Overview is the top of Metrics.
 */
export const observabilityRoutes = {
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
