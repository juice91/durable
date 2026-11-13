// Processors (guardrails) client: the catalog (GET /processors), the test console
// (POST /processors/test) and recorded outcomes (GET /processor-results), plus the pure helpers
// the Processors page uses.
//
// Outcomes carry match COUNTS only. The API never serves matched text and nothing here may
// suggest that it could.
import { ApiError, apiHeaders, BASE_URL, type Agent } from "./api";
import type { JsonSchema } from "./tools";

export type ProcessorType = "pii_redact" | "regex_blocklist" | "max_length" | "moderation";
export type ProcessorPhase = "input" | "output";
export type ProcessorAction = "allow" | "redact" | "block" | "truncate";

export const PROCESSOR_TYPES: ProcessorType[] = ["pii_redact", "regex_blocklist", "max_length", "moderation"];
export const PROCESSOR_PHASES: ProcessorPhase[] = ["input", "output"];
export const PROCESSOR_ACTIONS: ProcessorAction[] = ["allow", "redact", "block", "truncate"];

/** A JSON schema node as the catalog serves it (a superset of the tools form subset). */
export type ProcessorConfigSchema = JsonSchema & {
  properties?: Record<string, ProcessorConfigSchema>;
  items?: ProcessorConfigSchema;
  minimum?: number;
  maximum?: number;
  minItems?: number;
  maxItems?: number;
  minLength?: number;
  maxLength?: number;
};

export interface ProcessorDeclaration {
  id: ProcessorType;
  title: string;
  description: string;
  phases: ProcessorPhase[];
  actions: ProcessorAction[];
  /** False for types that call a model (moderation): not testable in the console. */
  deterministic: boolean;
  config_schema: ProcessorConfigSchema;
}

export interface ProcessorCatalog {
  items: ProcessorDeclaration[];
  limits: {
    max_processors_per_phase: number;
    max_regex_patterns: number;
    max_regex_pattern_length: number;
    max_test_sample_bytes: number;
  };
  moderation_categories: string[];
}

/** One processor as an agent spec (and the test console) declares it. */
export interface ProcessorSpec {
  type: ProcessorType;
  config: Record<string, unknown>;
}

export interface ProcessorOutcome {
  index: number;
  processor_type: ProcessorType;
  action: ProcessorAction;
  match_counts: Record<string, number>;
  reason?: string;
}

export interface ProcessorTestResult {
  /** The processed sample; null when a processor blocked it (blocked text is never passed on). */
  text: string | null;
  blocked: boolean;
  results: ProcessorOutcome[];
}

export interface ProcessorResultItem {
  run_id: string;
  seq: number;
  ts: string;
  agent_id: string;
  agent_version: number | null;
  parent_run_id: string | null;
  phase: ProcessorPhase | null;
  index: number | null;
  processor_type: ProcessorType | null;
  action: ProcessorAction | null;
  match_counts: Record<string, number>;
  reason?: string;
  categories?: string[];
}

export interface ProcessorResultsPage {
  items: ProcessorResultItem[];
  next_before: string | null;
}

export interface ProcessorResultFilters {
  agent_id?: string;
  type?: ProcessorType | "";
  action?: ProcessorAction | "";
  phase?: ProcessorPhase | "";
  run_id?: string;
  /** ISO timestamps (with offset). `from` is inclusive, `to` exclusive. */
  from?: string;
  to?: string;
  limit?: number;
  before?: string | null;
}

export const MODERATION_TEST_MESSAGE = "Moderation calls a model, so it cannot run in the test console. Test it in the playground.";

/**
 * A 422 validation list keeps each entry's field location ("processors.0.config.max_chars: …"),
 * without the leading "body"; any other error keeps the API's `detail` string.
 */
export function describeValidationDetail(detail: unknown): string | null {
  if (typeof detail === "string") return detail;
  if (!Array.isArray(detail)) return null;
  const parts = detail.map((entry: { loc?: unknown; msg?: unknown }) => {
    const loc = Array.isArray(entry?.loc) ? entry.loc.map(String) : [];
    const path = (loc[0] === "body" || loc[0] === "query" ? loc.slice(1) : loc).join(".");
    const msg = String(entry?.msg ?? "invalid");
    return path ? `${path}: ${msg}` : msg;
  });
  return parts.length ? parts.join("; ") : null;
}

async function processorApiError(response: Response): Promise<ApiError> {
  let message = `HTTP ${response.status}`;
  try {
    const data = await response.json();
    message = describeValidationDetail(data?.detail) ?? message;
  } catch {
    // Keep the HTTP status fallback when the response is not JSON.
  }
  return new ApiError(response.status, message);
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const response = await fetch(`${BASE_URL}/api/v1${path}`, {
    ...init,
    headers: { "Content-Type": "application/json", ...apiHeaders() },
  });
  if (!response.ok) throw await processorApiError(response);
  return response.json();
}

/** The query string for GET /processor-results; empty filters are left out. */
export function resultsQuery(filters: ProcessorResultFilters): string {
  const query = new URLSearchParams();
  const add = (key: string, value: string | number | null | undefined) => {
    if (value === undefined || value === null) return;
    const text = String(value).trim();
    if (text !== "") query.set(key, text);
  };
  add("agent_id", filters.agent_id);
  add("type", filters.type);
  add("action", filters.action);
  add("phase", filters.phase);
  add("run_id", filters.run_id);
  add("from", filters.from);
  add("to", filters.to);
  add("limit", filters.limit);
  add("before", filters.before);
  return query.toString();
}

export const processors = {
  catalog: () => request<ProcessorCatalog>("/processors"),
  test: (specs: ProcessorSpec[], text: string) =>
    request<ProcessorTestResult>("/processors/test", { method: "POST", body: JSON.stringify({ processors: specs, text }) }),
  results: (filters: ProcessorResultFilters = {}) => {
    const query = resultsQuery(filters);
    return request<ProcessorResultsPage>(`/processor-results${query ? `?${query}` : ""}`);
  },
};

/** A `datetime-local` input value (local time, no offset) as an ISO timestamp, or undefined. */
export function localInputToIso(value: string): string | undefined {
  if (!value) return undefined;
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? undefined : date.toISOString();
}

/** The API refuses moderation in the test console with this 422 message. */
export function isModerationRefusal(message: string): boolean {
  return /moderation.*test it in the playground/i.test(message);
}

/** "email ×2, phone ×1"; "No matches" when nothing matched. Counts only: never matched text. */
export function formatMatchCounts(counts: Record<string, number> | undefined): string {
  const entries = Object.entries(counts || {}).filter(([, n]) => n > 0);
  if (entries.length === 0) return "No matches";
  return entries.map(([kind, n]) => `${kind} ×${n}`).join(", ");
}

/** The config a new processor starts with: every property's schema default. */
export function defaultConfig(schema: ProcessorConfigSchema): Record<string, unknown> {
  const config: Record<string, unknown> = {};
  for (const [name, property] of Object.entries(schema.properties || {})) {
    if (property.default !== undefined) config[name] = structuredClone(property.default);
  }
  return config;
}

/** A short type label for one schema property ("string", "one of: a, b", "list of …"). */
export function describeSchemaType(schema: ProcessorConfigSchema): string {
  if (schema.enum) return `one of: ${schema.enum.join(", ")}`;
  if (schema.type === "array") {
    const items = schema.items ? describeSchemaType(schema.items) : "values";
    const bounds = [
      schema.minItems !== undefined ? `at least ${schema.minItems}` : null,
      schema.maxItems !== undefined ? `at most ${schema.maxItems}` : null,
    ].filter(Boolean).join(", ");
    return `list of ${items}${bounds ? ` (${bounds})` : ""}`;
  }
  if (schema.type === "object") {
    const keys = Object.keys(schema.properties || {});
    return keys.length ? `object { ${keys.join(", ")} }` : "object";
  }
  if (schema.type === "integer" || schema.type === "number") {
    const bounds = [
      schema.minimum !== undefined ? `≥ ${schema.minimum}` : null,
      schema.maximum !== undefined ? `≤ ${schema.maximum}` : null,
    ].filter(Boolean).join(", ");
    return `${schema.type}${bounds ? ` (${bounds})` : ""}`;
  }
  return schema.type ?? "value";
}

// ---------------------------------------------------------------------------------------------
// Which agents use each processor type

/** The processor fields of an agent spec (left out of the spec while empty). */
export interface SpecProcessors {
  input_processors?: ProcessorSpec[];
  output_processors?: ProcessorSpec[];
}

export interface ProcessorUsage {
  agent_id: string;
  agent_name: string;
  /** The latest published version that uses the type; null when only the draft does. */
  version: number | null;
  phases: ProcessorPhase[];
  /** The latest version could not be read, so whether it uses the type is unknown. */
  unknown?: boolean;
}

function phasesUsing(spec: SpecProcessors | null | undefined, type: ProcessorType): ProcessorPhase[] {
  if (!spec) return [];
  const phases: ProcessorPhase[] = [];
  if ((spec.input_processors || []).some((p) => p.type === type)) phases.push("input");
  if ((spec.output_processors || []).some((p) => p.type === type)) phases.push("output");
  return phases;
}

export interface AgentProcessorSpecs {
  agent: Pick<Agent, "id" | "name" | "latest_version">;
  draft: SpecProcessors | null;
  /** The latest published version's spec, when there is one. */
  latest: SpecProcessors | null;
  /** True when the latest version could not be read. */
  failed?: boolean;
}

/** For each type, the agents whose latest version (or, failing that, draft) uses it, sorted by name. */
export function processorUsage(agents: AgentProcessorSpecs[]): Record<ProcessorType, ProcessorUsage[]> {
  const usage = Object.fromEntries(PROCESSOR_TYPES.map((t) => [t, [] as ProcessorUsage[]])) as Record<ProcessorType, ProcessorUsage[]>;
  for (const { agent, draft, latest, failed } of agents) {
    for (const type of PROCESSOR_TYPES) {
      if (failed) {
        usage[type].push({ agent_id: agent.id, agent_name: agent.name, version: agent.latest_version, phases: [], unknown: true });
        continue;
      }
      const published = agent.latest_version !== null ? phasesUsing(latest, type) : [];
      if (published.length > 0) {
        usage[type].push({ agent_id: agent.id, agent_name: agent.name, version: agent.latest_version, phases: published });
        continue;
      }
      const drafted = phasesUsing(draft, type);
      if (drafted.length > 0) usage[type].push({ agent_id: agent.id, agent_name: agent.name, version: null, phases: drafted });
    }
  }
  for (const list of Object.values(usage)) list.sort((a, b) => a.agent_name.localeCompare(b.agent_name));
  return usage;
}

/** Maps `items` with at most `limit` calls of `fn` in flight; results keep the input order. */
export async function mapWithConcurrency<T, R>(items: T[], limit: number, fn: (item: T) => Promise<R>): Promise<R[]> {
  const results = new Array<R>(items.length);
  let next = 0;
  const worker = async () => {
    while (next < items.length) {
      const index = next++;
      results[index] = await fn(items[index]);
    }
  };
  await Promise.all(Array.from({ length: Math.min(Math.max(1, limit), items.length) }, worker));
  return results;
}

export const USAGE_CONCURRENCY = 4;

/**
 * Agents with their draft and latest published processor fields: one request per published agent,
 * at most `USAGE_CONCURRENCY` at a time. A version that cannot be read is marked `failed`.
 */
export async function loadAgentProcessorSpecs(agents: Agent[]): Promise<AgentProcessorSpecs[]> {
  return mapWithConcurrency(agents, USAGE_CONCURRENCY, async (agent): Promise<AgentProcessorSpecs> => {
    const draft = agent.draft_spec as unknown as SpecProcessors;
    if (agent.latest_version === null) return { agent, draft, latest: null };
    try {
      const version = await request<{ spec: SpecProcessors }>(
        `/agents/${encodeURIComponent(agent.id)}/versions/${agent.latest_version}`,
      );
      return { agent, draft, latest: version.spec };
    } catch {
      return { agent, draft, latest: null, failed: true };
    }
  });
}
