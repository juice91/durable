// Built-in trace view (P0.13): the GET /runs/{id}/trace client, span tree and waterfall geometry,
// span labels, number formatting, and the Traces list URL filters (the Metrics drill-down contract).
import { BASE_URL, apiErrorFromResponse, apiHeaders } from "./api";
import type { Run, RunKind } from "./runs";

export type SpanKind =
  | "run"
  | "child_run"
  | "model_request"
  | "tool_call"
  | "processor"
  | "workflow_step"
  // RAG and memory spans (RM.19): see eap_run_store.trace. Attributes are ids, counts, scores,
  // model, usage and cost; never memory or chunk text.
  | "retrieve"
  | "embedding"
  | "memory_recall"
  | "memory_index"
  | "observer"
  | "reflector"
  | "working_memory"
  | "ingest";

/** Every span kind, in legend order. */
export const SPAN_KINDS: readonly SpanKind[] = [
  "run", "child_run", "model_request", "tool_call", "processor", "workflow_step",
  "retrieve", "embedding", "memory_recall", "memory_index", "observer", "reflector", "working_memory", "ingest",
];

/** The RAG and memory span kinds (RM.19). */
export const RAG_SPAN_KINDS: ReadonlySet<SpanKind> = new Set<SpanKind>(["retrieve", "embedding", "ingest"]);
export const MEMORY_SPAN_KINDS: ReadonlySet<SpanKind> = new Set<SpanKind>([
  "memory_recall", "memory_index", "observer", "reflector", "working_memory",
]);

/** One retrieved source on a `retrieve` span (`attributes.hits`): never the chunk text. */
export interface RetrieveHit {
  n?: number;
  title?: string | null;
  /** The section's anchor URL (render only when `isSafeHttpUrl`). */
  url?: string | null;
  /** The URL, or `kb:{document_key}` when the document has no source URL. */
  ref?: string | null;
  kb_id?: string | null;
  document_id?: string | null;
  document_key?: string | null;
  chunk_id?: string | null;
  doc_version?: number | null;
  score?: number | null;
}

/** A knowledge base attached to the agent version (`attributes.knowledge_bases` of a retrieve span). */
export interface RetrieveKnowledgeBase {
  id: string;
  top_k: number | null;
  min_score: number | null;
}
export type SpanStatus = "ok" | "error" | "running" | "waiting" | "incomplete";

export interface SpanUsage {
  input_tokens: number | null;
  output_tokens: number | null;
  cache_read_tokens: number | null;
  cache_write_tokens: number | null;
  usage_measured: boolean;
}

export interface TraceSpan {
  id: string;
  parent_id: string | null;
  kind: SpanKind;
  name: string;
  start: string | null;
  end: string | null;
  status: SpanStatus;
  attributes: Record<string, unknown> & {
    model_id?: string | null;
    provider_model_name?: string | null;
    /** Decimal string in USD, null when unpriced. */
    cost_usd?: string | null;
    price_source?: string | null;
    usage?: SpanUsage | null;
    run_status?: string;
    display_status?: string;
  };
  input: unknown;
  output: unknown;
  /** Field name -> original size in bytes, for each field the API cut to a preview. */
  truncated: Record<string, number>;
}

export interface TraceTotals {
  runs: number;
  model_requests: number;
  tool_calls: number;
  input_tokens: number | null;
  output_tokens: number | null;
  cache_read_tokens: number | null;
  cache_write_tokens: number | null;
  cost_usd: string | null;
  unpriced_requests: number;
  unrecorded_requests: number;
  duration_ms: number | null;
  status: string;
  /** RM.19 (absent from older APIs): moderation, embedding and observer / reflector requests among
   * `model_requests`, and the embedding calls' input tokens and cost (null when one is unpriced). */
  moderation_requests?: number;
  embedding_requests?: number;
  memory_requests?: number;
  embedding_input_tokens?: number;
  embedding_cost_usd?: string | null;
}

export interface TraceTruncation {
  spans: boolean;
  events: boolean;
  child_runs: boolean;
  payload_fields: number;
}

export interface TraceLimits {
  max_spans: number;
  max_events_per_run: number;
  max_child_runs: number;
  field_max_bytes: number;
  payload_budget_bytes: number;
}

/** Run JSON with the usage fields added in P0.1 and the draft flag of P0.8. */
export type TraceRun = Run & {
  model_id?: string | null;
  model_overridden?: boolean;
  input_tokens?: number | null;
  output_tokens?: number | null;
  cost_usd?: string | null;
  started_at?: string | null;
  finished_at?: string | null;
  draft?: boolean;
};

export interface Trace {
  run: TraceRun;
  spans: TraceSpan[];
  totals: TraceTotals;
  truncated: TraceTruncation;
  limits: TraceLimits;
}

export async function getTrace(runId: string): Promise<Trace> {
  const response = await fetch(`${BASE_URL}/api/v1/runs/${encodeURIComponent(runId)}/trace`, {
    headers: apiHeaders(),
    cache: "no-store",
  });
  if (!response.ok) throw await apiErrorFromResponse(response);
  return response.json();
}

// ---------------------------------------------------------------------------- tree

export interface SpanRow {
  span: TraceSpan;
  depth: number;
  /** Number of direct children. */
  children: number;
}

/** Depth-first rows (children in API order) from a parents-first span list. A span whose parent
 * is missing becomes a root, so nothing is ever dropped. `collapsed` hides descendants. */
export function flattenSpans(spans: TraceSpan[], collapsed: ReadonlySet<string> = new Set()): SpanRow[] {
  const ids = new Set(spans.map((s) => s.id));
  const byParent = new Map<string | null, TraceSpan[]>();
  for (const span of spans) {
    const parent = span.parent_id !== null && ids.has(span.parent_id) && span.parent_id !== span.id ? span.parent_id : null;
    const list = byParent.get(parent) ?? [];
    list.push(span);
    byParent.set(parent, list);
  }
  const rows: SpanRow[] = [];
  const seen = new Set<string>();
  const visit = (span: TraceSpan, depth: number) => {
    if (seen.has(span.id)) return;
    seen.add(span.id);
    const kids = byParent.get(span.id) ?? [];
    rows.push({ span, depth, children: kids.length });
    if (collapsed.has(span.id)) return;
    for (const kid of kids) visit(kid, depth + 1);
  };
  for (const root of byParent.get(null) ?? []) visit(root, 0);
  return rows;
}

// ---------------------------------------------------------------------------- geometry

function ms(value: string | null | undefined): number | null {
  if (!value) return null;
  const t = Date.parse(value);
  return Number.isNaN(t) ? null : t;
}

export interface SpanGeometry {
  /** Percent of the trace window from its start. */
  offsetPct: number;
  /** Percent of the trace window; 0 for a zero-length span (drawn as a marker). */
  widthPct: number;
  durationMs: number | null;
  /** The span has not ended: its bar runs to the window's end. */
  open: boolean;
  zeroLength: boolean;
}

export interface TraceWindow {
  startMs: number;
  endMs: number;
  durationMs: number;
}

/** The window every bar is drawn against: earliest start to latest end (open spans end at `now`). */
export function traceWindow(spans: TraceSpan[], now: number = Date.now()): TraceWindow | null {
  let start = Infinity;
  let end = -Infinity;
  for (const span of spans) {
    const s = ms(span.start);
    if (s === null) continue;
    const e = ms(span.end) ?? Math.max(now, s);
    start = Math.min(start, s);
    end = Math.max(end, e, s);
  }
  if (!Number.isFinite(start)) return null;
  return { startMs: start, endMs: end, durationMs: Math.max(0, end - start) };
}

export function spanGeometry(span: TraceSpan, window: TraceWindow | null, now: number = Date.now()): SpanGeometry {
  const s = ms(span.start);
  const rawEnd = ms(span.end);
  const open = rawEnd === null;
  if (s === null || window === null) {
    return { offsetPct: 0, widthPct: 0, durationMs: null, open, zeroLength: false };
  }
  const e = rawEnd ?? Math.max(now, s);
  const duration = Math.max(0, e - s);
  if (window.durationMs <= 0) {
    return { offsetPct: 0, widthPct: duration > 0 ? 100 : 0, durationMs: open ? null : duration, open, zeroLength: duration === 0 };
  }
  const clamp = (v: number) => Math.min(100, Math.max(0, v));
  const offsetPct = clamp(((s - window.startMs) / window.durationMs) * 100);
  const widthPct = Math.min(100 - offsetPct, clamp((duration / window.durationMs) * 100));
  return { offsetPct, widthPct, durationMs: open ? null : duration, open, zeroLength: duration === 0 };
}

// ---------------------------------------------------------------------------- labels

function short(id: string): string {
  return id.length > 8 ? `${id.slice(0, 8)}...` : id;
}

function str(value: unknown): string | null {
  return typeof value === "string" && value !== "" ? value : null;
}

export interface NameMaps {
  agentNames?: Record<string, string>;
  workflowNames?: Record<string, string>;
  /** Knowledge base id -> name (kb_ingest runs, retrieve hits). */
  knowledgeBaseNames?: Record<string, string>;
}

function num(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

/** A readable span label. Run spans' API names are ids, so agents and workflows are looked up by
 * name (falling back to a short id); other spans keep the API name. */
export function spanLabel(
  span: TraceSpan,
  { agentNames = {}, workflowNames = {}, knowledgeBaseNames = {} }: NameMaps = {},
): string {
  const a = span.attributes ?? {};
  if (span.kind === "run" || span.kind === "child_run") {
    const kind = str(a.run_kind);
    if (kind === "tool") return `Tool: ${str(a.tool_id) ?? "unknown"}`;
    if (kind === "kb_ingest") {
      const id = str(a.knowledge_base_id);
      const name = id ? knowledgeBaseNames[id] : undefined;
      return name ? `Knowledge ingest: ${name}` : "Knowledge ingest";
    }
    if (kind === "workflow") {
      const id = str(a.workflow_id);
      const name = id ? (workflowNames[id] ?? short(id)) : "unknown";
      return `Workflow: ${name}${a.workflow_version != null ? ` v${a.workflow_version}` : ""}`;
    }
    const id = str(a.agent_id);
    const name = id ? (agentNames[id] ?? short(id)) : "unknown";
    let label = `Agent: ${name}${a.agent_version != null ? ` v${a.agent_version}` : ""}`;
    if (a.turn != null) label += ` (turn ${a.turn})`;
    return label;
  }
  if (span.kind === "model_request") {
    const n = typeof a.model_request === "number" ? ` #${a.model_request}` : "";
    return `Model request${n}: ${str(a.model_id) ?? span.name}`;
  }
  if (span.kind === "tool_call") return `Tool call: ${span.name}`;
  if (span.kind === "processor") return `Processor: ${span.name}${str(a.phase) ? ` (${a.phase})` : ""}`;
  if (span.kind === "workflow_step") return `Step: ${span.name}${str(a.step_type) ? ` (${a.step_type})` : ""}`;
  if (span.kind === "retrieve") {
    const kb = str(a.knowledge_base);
    const hits = num(a.hit_count);
    return `Retrieve${kb ? ` from ${kb}` : ""}${hits !== null ? ` (${hits} hit${hits === 1 ? "" : "s"})` : ""}`;
  }
  if (span.kind === "embedding") {
    const batch = num(a.batch);
    return `Embedding${batch !== null ? ` batch ${batch}` : ""}: ${str(a.model_id) ?? "unknown model"}`;
  }
  if (span.kind === "memory_recall") {
    const hits = num(a.hits);
    return `Semantic recall${hits !== null ? ` (${hits} hit${hits === 1 ? "" : "s"})` : ""}`;
  }
  if (span.kind === "memory_index") return "Memory index";
  if (span.kind === "observer") return "Observer";
  if (span.kind === "reflector") return "Reflector";
  if (span.kind === "working_memory") {
    const version = num(a.version);
    return `Working memory update${version !== null ? ` (v${version})` : ""}`;
  }
  if (span.kind === "ingest") return `Ingest: ${span.name}`;
  return span.name;
}

export const KIND_LABELS: Record<SpanKind, string> = {
  run: "Run",
  child_run: "Child run",
  model_request: "Model request",
  tool_call: "Tool call",
  processor: "Processor",
  workflow_step: "Workflow step",
  retrieve: "Retrieve",
  embedding: "Embedding",
  memory_recall: "Semantic recall",
  memory_index: "Memory index",
  observer: "Observer",
  reflector: "Reflector",
  working_memory: "Working memory",
  ingest: "Ingest phase",
};

// ---------------------------------------------------------------------------- formatting

/** The shared duration format (`lib/format.ts`), re-exported for the trace modules. */
export { formatDuration } from "./format";

/** Run duration: started_at → finished_at when recorded, else created_at → updated_at for a finished run. */
export function runDurationMs(run: TraceRun): number | null {
  const terminal = ["completed", "failed", "needs_attention"].includes(run.display_status);
  const start = ms(run.started_at) ?? ms(run.created_at);
  const end = ms(run.finished_at) ?? (terminal ? ms(run.updated_at) : null);
  if (start === null || end === null) return null;
  return Math.max(0, end - start);
}

// ---------------------------------------------------------------------------- list filters

/** Traces list URL filters: the Metrics drill-down contract (`agent_id`, `model`, `status`,
 * `from`, `to`, `kind`) plus `provider`. `status` may hold several, comma-separated. */
export interface TraceFilters {
  status: string[];
  from: string | null;
  to: string | null;
  model: string | null;
  provider: string | null;
  agent_id: string | null;
  kind: RunKind | null;
}

export const EMPTY_FILTERS: TraceFilters = {
  status: [], from: null, to: null, model: null, provider: null, agent_id: null, kind: null,
};

const KINDS: ReadonlySet<string> = new Set<RunKind>(["agent", "tool", "workflow", "kb_ingest"]);

export function parseTraceFilters(params: URLSearchParams | { get(name: string): string | null; getAll?(name: string): string[] }): TraceFilters {
  const one = (name: string) => {
    const v = params.get(name);
    return v && v.trim() !== "" ? v.trim() : null;
  };
  const rawStatus = params.getAll ? params.getAll("status") : [params.get("status") ?? ""];
  const status = Array.from(
    new Set(rawStatus.flatMap((s) => (s ?? "").split(",")).map((s) => s.trim()).filter(Boolean)),
  );
  const kind = one("kind");
  return {
    status,
    from: one("from"),
    to: one("to"),
    model: one("model"),
    provider: one("provider"),
    agent_id: one("agent_id"),
    kind: kind && KINDS.has(kind) ? (kind as TraceFilters["kind"]) : null,
  };
}

/** The URL query (and the API query) for a set of filters; empty filters are left out. */
export function traceFiltersToParams(filters: TraceFilters): URLSearchParams {
  const params = new URLSearchParams();
  if (filters.status.length) params.set("status", filters.status.join(","));
  for (const key of ["from", "to", "model", "provider", "agent_id", "kind"] as const) {
    const v = filters[key];
    if (v) params.set(key, v);
  }
  return params;
}

export function hasActiveFilters(filters: TraceFilters): boolean {
  return traceFiltersToParams(filters).toString() !== "";
}

export async function listTraceRuns(
  filters: TraceFilters,
  { limit = 50, before, signal }: { limit?: number; before?: string; signal?: AbortSignal } = {},
): Promise<{ items: TraceRun[]; next_before: string | null }> {
  const params = traceFiltersToParams(filters);
  params.set("limit", String(limit));
  if (before) params.set("before", before);
  const response = await fetch(`${BASE_URL}/api/v1/runs?${params.toString()}`, { headers: apiHeaders(), signal });
  if (!response.ok) throw await apiErrorFromResponse(response);
  return response.json();
}
