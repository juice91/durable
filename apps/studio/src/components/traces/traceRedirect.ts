import { legacyTarget, RouteIdError, routes } from "@/lib/routes";

/**
 * Where a retired trace or run page (`/traces/:runId`, `/runs/:id`) goes: the legacy table's row
 * (SL.12, `TRACE_LEGACY_RULES`), else the Traces list when the id cannot be a path segment.
 * `path` builds the old pathname; it throws RouteIdError for "." and "..".
 */
export function traceRedirectTarget(path: () => string, searchParams?: Record<string, string | string[] | undefined> | URLSearchParams | null): string {
  let pathname: string;
  try {
    pathname = path();
  } catch (error) {
    if (error instanceof RouteIdError) return routes.traces();
    throw error;
  }
  // SL.12's rows are in the shared table (LEGACY_RULES spreads TRACE_LEGACY_RULES).
  return legacyTarget(pathname, searchParams) ?? routes.traces();
}
