// The flat spans list (GET /spans): one row per materialised span of a finished run, newest first.
import { BASE_URL, apiErrorFromResponse, apiHeaders } from "./api";
import { EMPTY_FILTERS, SPAN_KINDS, parseTraceFilters, traceFiltersToParams, type SpanKind, type SpanStatus, type TraceFilters } from "./traces";

export interface SpanRow {
  run_id: string;
  span_id: string;
  parent_id: string | null;
  kind: SpanKind;
  name: string;
  start: string | null;
  end: string | null;
  status: SpanStatus;
  agent_id: string | null;
  workflow_id: string | null;
  model: string | null;
  /** `external`: sent over OTLP; `trace_id` is then the client's and `run_id` a derived id no run has. */
  source?: "platform" | "external";
  trace_id?: string | null;
  service_name?: string | null;
}

/** The Traces filters plus the span's own kind. */
export interface SpanFilters extends TraceFilters {
  span_kind: SpanKind | null;
}

export const EMPTY_SPAN_FILTERS: SpanFilters = { ...EMPTY_FILTERS, span_kind: null };

export function parseSpanFilters(params: URLSearchParams): SpanFilters {
  const kind = params.get("span_kind")?.trim() ?? "";
  return { ...parseTraceFilters(params), span_kind: (SPAN_KINDS as readonly string[]).includes(kind) ? (kind as SpanKind) : null };
}

export function spanFiltersToParams(filters: SpanFilters): URLSearchParams {
  const params = traceFiltersToParams(filters);
  if (filters.span_kind) params.set("span_kind", filters.span_kind);
  return params;
}

export async function listSpans(
  filters: SpanFilters,
  { limit = 50, before, signal }: { limit?: number; before?: string; signal?: AbortSignal } = {},
): Promise<{ items: SpanRow[]; next_before: string | null }> {
  const params = spanFiltersToParams(filters);
  params.set("limit", String(limit));
  if (before) params.set("before", before);
  const response = await fetch(`${BASE_URL}/api/v1/spans?${params.toString()}`, { headers: apiHeaders(), signal });
  if (!response.ok) throw await apiErrorFromResponse(response);
  return response.json();
}
