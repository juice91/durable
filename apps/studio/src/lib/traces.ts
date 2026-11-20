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
  | "ingest"
  // Agent capability spans (AC.17): see eap_run_store.trace. Never error, argument or output text.
  | "fallback_attempt"
  | "tool_approval"
  | "output_retry"
  | "eval_case"
  | "judge"
  // Workflow loop spans (LP.9): a loop or foreach step, and one span per iteration.
  | "loop"
  | "loop_iteration"
  // LP.15: a workflow evaluate step (verdict, evaluator results, judge usage; never text).
  | "evaluate"
  // A span sent over OTLP by a program outside the platform (GET /spans, source "external").
  | "external";

/** Every span kind, in legend order. */
export const SPAN_KINDS: readonly SpanKind[] = [
  "run", "child_run", "model_request", "tool_call", "processor", "workflow_step",
  "retrieve", "embedding", "memory_recall", "memory_index", "observer", "reflector", "working_memory", "ingest",
  "fallback_attempt", "tool_approval", "output_retry", "eval_case", "judge",
  "loop", "loop_iteration", "evaluate", "external",
];

/** The agent capability span kinds (AC.17). */
export const CAPABILITY_SPAN_KINDS: ReadonlySet<SpanKind> = new Set<SpanKind>([
  "fallback_attempt", "tool_approval", "output_retry", "eval_case", "judge",
]);

/** One failed attempt of a model request on a fallback chain (`attributes.failed_attempts`). */
export interface FailedAttempt {
  model_id?: string | null;
  outcome?: string | null;
  error_type?: string | null;
  http_status?: number | null;
}

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
  /** RM.19 follow-up (absent from older APIs): the usage split by operation. `chat_*` is the agent's
   * model only; chat + memory + moderation + embedding input tokens equal `input_tokens`. Tokens are
   * null when the group had requests but none was recorded; a cost is null when one is unpriced. */
  chat_requests?: number;
  chat_input_tokens?: number | null;
  chat_output_tokens?: number | null;
  chat_cost_usd?: string | null;
  memory_input_tokens?: number | null;
  memory_output_tokens?: number | null;
  memory_cost_usd?: string | null;
  moderation_input_tokens?: number | null;
  moderation_output_tokens?: number | null;
  moderation_cost_usd?: string | null;
  /** AC.17 (absent from older APIs): an eval run's LLM-judge requests, apart from `chat_*`. */
  judge_requests?: number;
  judge_input_tokens?: number | null;
  judge_output_tokens?: number | null;
  judge_cost_usd?: string | null;
}

/** One non-chat group of a usage split (memory: observer and reflector; moderation; embedding;
 * judge: an eval run's LLM-judge requests, AC.17). */
export interface UsageGroup {
  id: "memory" | "moderation" | "embedding" | "judge";
  label: string;
  inputTokens: number | null;
  /** Embeddings have no output tokens: null. */
  outputTokens: number | null;
  /** Undefined where the source carries no cost (a run span); null when unpriced. */
  costUsd?: string | null;
}

export interface UsageSplit {
  chat: { inputTokens: number | null; outputTokens: number | null; costUsd?: string | null };
  /** The other groups that used any tokens (or had requests), in a fixed order. */
  others: UsageGroup[];
}

const GROUP_LABELS = { memory: "Memory (observer, reflector)", moderation: "Moderation", embedding: "Embedding", judge: "Judge" } as const;

function tokenField(source: Record<string, unknown>, key: string): number | null {
  const value = source[key];
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

/**
 * The usage split of trace totals or a run span's attributes, or null when the response predates it
 * (no `chat_input_tokens` key): callers then show the combined tokens as before. A group is listed
 * when it has tokens, a cost, or requests (`{group}_requests` on the totals).
 */
export function usageSplit(source: Record<string, unknown> | null | undefined): UsageSplit | null {
  if (!source || !("chat_input_tokens" in source)) return null;
  const cost = (key: string): string | null | undefined => (key in source ? (typeof source[key] === "string" ? (source[key] as string) : null) : undefined);
  const others: UsageGroup[] = [];
  for (const id of ["memory", "moderation", "embedding", "judge"] as const) {
    const inputTokens = tokenField(source, `${id}_input_tokens`);
    const outputTokens = id === "embedding" ? null : tokenField(source, `${id}_output_tokens`);
    const costUsd = cost(`${id}_cost_usd`);
    const requests = tokenField(source, `${id}_requests`) ?? 0;
    const used = (inputTokens ?? 0) > 0 || (outputTokens ?? 0) > 0 || requests > 0;
    if (!used) continue;
    others.push({ id, label: GROUP_LABELS[id], inputTokens, outputTokens, ...(costUsd !== undefined ? { costUsd } : {}) });
  }
  const chatCost = cost("chat_cost_usd");
  return {
    chat: { inputTokens: tokenField(source, "chat_input_tokens"), outputTokens: tokenField(source, "chat_output_tokens"), ...(chatCost !== undefined ? { costUsd: chatCost } : {}) },
    others,
  };
}

export interface TraceTruncation {
  spans: boolean;
  events: boolean;
  child_runs: boolean;
  payload_fields: number;
  /** LP.9: loop iterations left out past `limits.max_loop_iteration_spans` per loop, or past
   * `limits.max_loop_trace_events` in all (absent on older APIs). */
  iterations?: number;
}

export interface TraceLimits {
  max_spans: number;
  max_events_per_run: number;
  max_child_runs: number;
  field_max_bytes: number;
  payload_budget_bytes: number;
  max_loop_iteration_spans?: number;
  max_loop_trace_events?: number;
}

/** Run JSON with the usage fields added in P0.1 and the draft flag of P0.8. */
export type TraceRun = Run & {
  /** The requested model (the agent's, or a per-run override). */
  model_id?: string | null;
  /** The model that answered the run's last chat request: a fallback model when one did. Null
   * before any chat request is recorded (and from an older API). */
  answering_model_id?: string | null;
  model_overridden?: boolean;
  input_tokens?: number | null;
  output_tokens?: number | null;
  cost_usd?: string | null;
  started_at?: string | null;
  finished_at?: string | null;
  draft?: boolean;
  /** Tags derived from what the run records (kind, agent, model, draft, context keys). */
  tags?: string[];
  request_context?: Record<string, unknown>;
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

/**
 * The rows whose label (`spanLabel`) or kind contains `query`, case-insensitively, plus the
 * ancestors of every match so the tree stays readable. `rows` come from `flattenSpans` (depth first).
 * A blank query keeps all rows.
 */
export function filterSpanRows(rows: SpanRow[], query: string, names: NameMaps = {}): SpanRow[] {
  const needle = query.trim().toLowerCase();
  if (!needle) return rows;
  const keep = new Set<number>();
  rows.forEach((row, index) => {
    const label = spanLabel(row.span, names).toLowerCase();
    if (!label.includes(needle) && !row.span.kind.toLowerCase().includes(needle)) return;
    keep.add(index);
    let depth = row.depth;
    for (let i = index - 1; i >= 0 && depth > 0; i--) {
      if (rows[i].depth < depth) {
        keep.add(i);
        depth = rows[i].depth;
      }
    }
  });
  return rows.filter((_, index) => keep.has(index));
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
    // A draft run has no version (follow-up 1c): "Draft" where the version would be.
    let label = `Agent: ${name}${a.draft === true ? " · Draft" : a.agent_version != null ? ` v${a.agent_version}` : ""}`;
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
  if (span.kind === "fallback_attempt") {
    const k = num(a.fallback_position);
    return `Failed attempt${k !== null ? ` ${k + 1}` : ""}: ${str(a.model_id) ?? span.name}`;
  }
  if (span.kind === "tool_approval") return `Approval: ${str(a.tool_name) ?? "tool call"}`;
  if (span.kind === "output_retry") {
    const n = num(a.model_request);
    const errors = num(a.error_count);
    return `Output retry${n !== null ? ` after #${n}` : ""}${errors !== null ? ` (${errors} error${errors === 1 ? "" : "s"})` : ""}`;
  }
  if (span.kind === "eval_case") {
    const position = num(a.position);
    return `Case ${position !== null ? position + 1 : str(a.case_id) ?? ""}${str(a.case_status) ? `: ${a.case_status}` : ""}`.trim();
  }
  if (span.kind === "loop") {
    const done = num(a.iterations_completed);
    return `Loop: ${span.name}${str(a.step_type) ? ` (${a.step_type})` : ""}${done !== null ? `, ${done} done` : ""}`;
  }
  if (span.kind === "loop_iteration") {
    const i = num(a.index);
    return `Iteration ${i !== null ? i + 1 : "?"}: ${str(a.step_id) ?? span.name}`;
  }
  if (span.kind === "evaluate") {
    const verdict = a.passed === true ? "passed" : a.passed === false ? "failed" : null;
    return `Evaluate: ${span.name}${verdict ? ` (${verdict})` : ""}`;
  }
  if (span.kind === "judge") {
    const i = num(a.evaluator_index);
    return `Judge${i !== null ? ` (evaluator ${i + 1})` : ""}: ${str(a.model_id) ?? "unknown model"}`;
  }
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
  fallback_attempt: "Fallback attempt",
  tool_approval: "Tool approval",
  output_retry: "Output retry",
  eval_case: "Test case",
  judge: "Judge request",
  loop: "Loop",
  loop_iteration: "Loop iteration",
  evaluate: "Evaluate step",
  external: "External span",
};

// ---------------------------------------------------------------------------- formatting

/** The shared duration format (`lib/format.ts`), re-exported for the trace modules. */
export { formatDuration } from "./format";

/** Run duration: started_at → finished_at when recorded, else created_at → updated_at for a finished run. */
export function runDurationMs(run: TraceRun): number | null {
  const terminal = ["completed", "skipped", "failed", "needs_attention"].includes(run.display_status);
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
  /** Derived run tags (`kind:agent`, `draft`, `ctx:<key>`...); a run must carry all of them. */
  tags: string[];
  /** `key=value` pairs matched against the run's request context; all must match. */
  metadata: string[];
  /** Free text over the run's input, output, error, tool id and model id. */
  q: string | null;
  /** Platform runs, external OTLP traces, or both (`all`, the default). Not a `GET /runs` filter. */
  source: TraceSource;
}

export type TraceSource = "all" | "platform" | "external";
export const TRACE_SOURCES: readonly TraceSource[] = ["all", "platform", "external"];
export const TRACE_SOURCE_LABELS: Record<TraceSource, string> = { all: "All", platform: "Platform", external: "External" };

export const EMPTY_FILTERS: TraceFilters = {
  status: [], from: null, to: null, model: null, provider: null, agent_id: null, kind: null,
  tags: [], metadata: [], q: null, source: "all",
};

const KINDS: ReadonlySet<string> = new Set<RunKind>(["agent", "tool", "workflow", "kb_ingest", "eval"]);

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
    tags: Array.from(new Set((params.getAll ? params.getAll("tags") : [params.get("tags") ?? ""]).flatMap((t) => (t ?? "").split(",")).map((t) => t.trim()).filter(Boolean))),
    metadata: Array.from(new Set((params.getAll ? params.getAll("metadata") : [params.get("metadata") ?? ""]).map((m) => (m ?? "").trim()).filter((m) => m.includes("=")))),
    q: one("q"),
    source: ((v) => (v === "platform" || v === "external" ? v : "all"))(one("source")),
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
  if (filters.tags.length) params.set("tags", filters.tags.join(","));
  for (const pair of filters.metadata) params.append("metadata", pair);
  if (filters.q) params.set("q", filters.q);
  if (filters.source !== "all") params.set("source", filters.source);
  return params;
}

/** The `GET /runs` query: the filters without `source` (which only picks what the page lists). */
export function runQueryParams(filters: TraceFilters): URLSearchParams {
  const params = traceFiltersToParams(filters);
  params.delete("source");
  return params;
}

/** Whether a filter that only runs have is set (an external trace matches none of them). */
export function hasRunOnlyFilters(filters: TraceFilters): boolean {
  return Boolean(
    filters.status.length || filters.model || filters.provider || filters.agent_id || filters.kind ||
      filters.tags.length || filters.metadata.length,
  );
}

export function hasActiveFilters(filters: TraceFilters): boolean {
  return traceFiltersToParams(filters).toString() !== "";
}

export async function listTraceRuns(
  filters: TraceFilters,
  { limit = 50, before, signal }: { limit?: number; before?: string; signal?: AbortSignal } = {},
): Promise<{ items: TraceRun[]; next_before: string | null }> {
  const params = runQueryParams(filters);
  params.set("limit", String(limit));
  if (before) params.set("before", before);
  const response = await fetch(`${BASE_URL}/api/v1/runs?${params.toString()}`, { headers: apiHeaders(), signal });
  if (!response.ok) throw await apiErrorFromResponse(response);
  return response.json();
}

export interface RunsHistogramBucket {
  start: string;
  runs: number;
  failed: number;
}

export interface RunsHistogram {
  interval: "hour" | "day";
  buckets: RunsHistogramBucket[];
}

/** GET /runs/histogram for the list filters: zero-filled buckets, hourly under 72 h, else daily. */
export async function fetchRunsHistogram(filters: TraceFilters, signal?: AbortSignal): Promise<RunsHistogram> {
  const params = runQueryParams(filters);
  const response = await fetch(`${BASE_URL}/api/v1/runs/histogram?${params.toString()}`, { headers: apiHeaders(), signal });
  if (!response.ok) throw await apiErrorFromResponse(response);
  return response.json();
}

/**
 * A run's model as Studio shows it (the Traces lists and the run span's Run usage): the model that
 * answered, and the requested one only where they differ (a fallback model answered). Without an
 * answering model (no request recorded yet, an older API) it is the requested model alone.
 */
export function runModels(source: { model_id?: unknown; answering_model_id?: unknown }): { model: string | null; requested: string | null } {
  const requested = typeof source.model_id === "string" && source.model_id ? source.model_id : null;
  const answering = typeof source.answering_model_id === "string" && source.answering_model_id ? source.answering_model_id : null;
  if (!answering) return { model: requested, requested: null };
  return { model: answering, requested: requested && requested !== answering ? requested : null };
}


// ---------------------------------------------------------------------------- feedback (W2.2)

export type FeedbackRating = "up" | "down";

/** One piece of feedback on a trace (a run) or one of its spans (`span_id`), with who and when. */
export interface TraceFeedback {
  id: string;
  trace_id: string;
  span_id: string | null;
  rating: FeedbackRating;
  score: number | null;
  comment: string | null;
  created_by: string;
  created_at: string;
}

export interface FeedbackInput {
  rating: FeedbackRating;
  comment?: string | null;
  score?: number | null;
  span_id?: string | null;
}

export async function listTraceFeedback(traceId: string, opts: { spanId?: string | null; signal?: AbortSignal } = {}): Promise<TraceFeedback[]> {
  const query = opts.spanId ? `?${new URLSearchParams({ span_id: opts.spanId })}` : "";
  const response = await fetch(`${BASE_URL}/api/v1/traces/${encodeURIComponent(traceId)}/feedback${query}`, { headers: apiHeaders(), signal: opts.signal });
  if (!response.ok) throw await apiErrorFromResponse(response);
  return ((await response.json()) as { items: TraceFeedback[] }).items;
}

export async function addTraceFeedback(traceId: string, input: FeedbackInput): Promise<TraceFeedback> {
  const response = await fetch(`${BASE_URL}/api/v1/traces/${encodeURIComponent(traceId)}/feedback`, {
    method: "POST",
    headers: apiHeaders({ "Content-Type": "application/json" }),
    body: JSON.stringify(input),
  });
  if (!response.ok) throw await apiErrorFromResponse(response);
  return response.json();
}

/** localStorage key: the trace list columns the viewer has hidden (listed in Settings, browser data). */
export const TRACE_COLUMNS_STORAGE_KEY = "studio.traces.hiddenColumns";
