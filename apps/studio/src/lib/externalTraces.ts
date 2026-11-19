// External OpenTelemetry traces (sent with a tenant API key to POST /api/v1/otlp/v1/traces): the
// list and one trace, read back through GET /traces and GET /traces/{trace_id}.
import { BASE_URL, apiErrorFromResponse, apiHeaders } from "./api";
import type { TraceFilters } from "./traces";

export interface ExternalTrace {
  trace_id: string;
  source: "external";
  /** The root span's name. */
  name: string | null;
  /** The resource's `service.name`. */
  service_name: string | null;
  start: string;
  end: string | null;
  span_count: number;
  status: "ok" | "error";
}

export interface ExternalSpan {
  span_id: string;
  parent_id: string | null;
  name: string;
  start: string;
  end: string | null;
  status: string;
  attributes: Record<string, unknown>;
}

export interface ExternalTraceDetail {
  trace_id: string;
  source: "external";
  service_name: string | null;
  resource_attributes: Record<string, unknown>;
  spans: ExternalSpan[];
  /** The platform run named by a span's `pydanticos.run_id` attribute, when it is this tenant's. */
  run_id: string | null;
  truncated: boolean;
}

/** The attribute that ties an external trace to a platform run. */
export const RUN_ID_ATTRIBUTE = "pydanticos.run_id";

export async function listExternalTraces(
  filters: Pick<TraceFilters, "q" | "from" | "to">,
  { limit = 50, before, signal }: { limit?: number; before?: string; signal?: AbortSignal } = {},
): Promise<{ items: ExternalTrace[]; next_before: string | null }> {
  const params = new URLSearchParams({ limit: String(limit) });
  if (filters.q) params.set("q", filters.q);
  if (filters.from) params.set("from", filters.from);
  if (filters.to) params.set("to", filters.to);
  if (before) params.set("before", before);
  const response = await fetch(`${BASE_URL}/api/v1/traces?${params.toString()}`, { headers: apiHeaders(), signal });
  if (!response.ok) throw await apiErrorFromResponse(response);
  return response.json();
}

export async function getExternalTrace(traceId: string, signal?: AbortSignal): Promise<ExternalTraceDetail> {
  const response = await fetch(`${BASE_URL}/api/v1/traces/${encodeURIComponent(traceId)}`, { headers: apiHeaders(), signal });
  if (!response.ok) throw await apiErrorFromResponse(response);
  return response.json();
}

export interface ExternalSpanRow {
  span: ExternalSpan;
  depth: number;
}

/** Depth-first rows from a trace's spans (start order). A span whose parent is not in the list is a root. */
export function externalSpanRows(spans: ExternalSpan[]): ExternalSpanRow[] {
  const ids = new Set(spans.map((span) => span.span_id));
  const children = new Map<string | null, ExternalSpan[]>();
  for (const span of spans) {
    const parent = span.parent_id && ids.has(span.parent_id) && span.parent_id !== span.span_id ? span.parent_id : null;
    children.set(parent, [...(children.get(parent) ?? []), span]);
  }
  const rows: ExternalSpanRow[] = [];
  const seen = new Set<string>();
  const visit = (span: ExternalSpan, depth: number) => {
    if (seen.has(span.span_id)) return;
    seen.add(span.span_id);
    rows.push({ span, depth });
    for (const child of children.get(span.span_id) ?? []) visit(child, depth + 1);
  };
  for (const root of children.get(null) ?? []) visit(root, 0);
  return rows;
}

/** A trace's duration: first span start to last span end, or null while no end is known. */
export function externalDurationMs(trace: Pick<ExternalTrace, "start" | "end">): number | null {
  if (!trace.end) return null;
  const ms = new Date(trace.end).getTime() - new Date(trace.start).getTime();
  return Number.isFinite(ms) ? Math.max(0, ms) : null;
}
