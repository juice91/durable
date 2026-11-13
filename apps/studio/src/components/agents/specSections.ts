/**
 * Agent spec sections added in milestone P0 (task P0.14): input and output processors, and request
 * context fields. Wire shapes follow eap_schemas `processors.py` and `request_context.py`.
 *
 * Like `mcp_tools`, the three lists are left out of a saved spec while empty, so specs without them
 * keep their exact JSON and `spec_hash`.
 */
import { apiErrorFromResponse, apiHeaders, BASE_URL, type AgentSpec } from "@/lib/api";
import type { JsonSchema } from "@/lib/tools";

export type ProcessorPhase = "input" | "output";
export type ProcessorType = "pii_redact" | "regex_blocklist" | "max_length" | "moderation";

/** One processor as stored in the spec: `{"type": ..., "config": {...}}`. */
export interface ProcessorSpec {
  type: ProcessorType | string;
  config: Record<string, unknown>;
}

/**
 * A processor while it is edited: `clientId` is a stable, editor-only key (list keys and server
 * error placement survive reordering). It is never saved: `normalizeProcessor` drops it.
 */
export interface EditableProcessor extends ProcessorSpec {
  clientId: string;
}

let clientIdCounter = 0;

export function newClientId(): string {
  clientIdCounter += 1;
  return `p${clientIdCounter}`;
}

export function withClientIds(processors: ProcessorSpec[]): EditableProcessor[] {
  return processors.map((processor) => ({ type: processor.type, config: processor.config, clientId: newClientId() }));
}

export type ContextFieldType = "string" | "integer" | "number" | "boolean" | "enum";

export interface ContextField {
  name: string;
  type: ContextFieldType;
  required?: boolean;
  description?: string;
  /** Strings only. */
  max_length?: number | null;
  /** Enums only. */
  values?: string[];
}

export type AgentSpecWithSections = AgentSpec & {
  input_processors?: ProcessorSpec[];
  output_processors?: ProcessorSpec[];
  context_fields?: ContextField[];
};

export interface SpecSections {
  input_processors: ProcessorSpec[];
  output_processors: ProcessorSpec[];
  context_fields: ContextField[];
}

/** Catalog schema: JsonSchema plus the bounds the API emits. */
export type CatalogSchema = JsonSchema & {
  properties?: Record<string, CatalogSchema>;
  items?: CatalogSchema;
  minItems?: number;
  maxItems?: number;
  minLength?: number;
  maxLength?: number;
  additionalProperties?: boolean;
  minimum?: number;
  maximum?: number;
};

export interface ProcessorDeclaration {
  id: ProcessorType;
  title: string;
  description: string;
  phases: ProcessorPhase[];
  actions: string[];
  deterministic: boolean;
  config_schema: CatalogSchema;
}

export interface ProcessorCatalog {
  items: ProcessorDeclaration[];
  limits: {
    max_processors_per_phase: number;
    max_regex_patterns: number;
    max_regex_pattern_length: number;
    max_test_sample_bytes?: number;
  };
  moderation_categories: string[];
}

export interface ModelOption {
  id: string;
  provider: string;
  name: string;
  /** A `provider:*` allowlist entry: the name is typed freely. */
  wildcard: boolean;
  source: "allowlist" | "scripted" | string;
  description: string | null;
}

export const MAX_CONTEXT_FIELDS = 32;
export const CONTEXT_NAME_PATTERN = /^[a-z][a-z0-9_]{0,31}$/;
export const RESERVED_CONTEXT_NAMES = ["tenant_id", "user_id", "run_id"];
export const DEFAULT_MAX_PROCESSORS_PER_PHASE = 8;

async function getJson<T>(path: string): Promise<T> {
  const response = await fetch(`${BASE_URL}${path}`, { headers: apiHeaders({ "Content-Type": "application/json" }) });
  if (!response.ok) throw await apiErrorFromResponse(response);
  return response.json() as Promise<T>;
}

/** `GET /api/v1/processors`. */
export function fetchProcessorCatalog(): Promise<ProcessorCatalog> {
  return getJson<ProcessorCatalog>("/api/v1/processors");
}

/** `GET /api/v1/models` (allowlist entries and the scripted `test:*` models). */
export async function fetchModels(): Promise<ModelOption[]> {
  const data = await getJson<{ items: ModelOption[] }>("/api/v1/models");
  return data.items;
}

/** The sections of a stored spec, with absent lists as empty ones. */
export function sectionsOf(spec: AgentSpec | undefined): SpecSections {
  const s = spec as AgentSpecWithSections | undefined;
  return {
    input_processors: s?.input_processors ?? [],
    output_processors: s?.output_processors ?? [],
    context_fields: s?.context_fields ?? [],
  };
}

/** A processor as it is saved: `patterns` without blank lines, nothing else changed. */
export function normalizeProcessor(processor: ProcessorSpec): ProcessorSpec {
  const config = { ...processor.config };
  if (Array.isArray(config.patterns)) config.patterns = (config.patterns as unknown[]).filter((p) => typeof p === "string" && p.length > 0);
  return { type: processor.type, config };
}

/**
 * A context field as it is saved, in the server's full dump shape (so a loaded draft round-trips
 * unchanged): `max_length` only for strings, `values` (without blanks) only for enums.
 */
export function normalizeContextField(field: ContextField): Required<ContextField> {
  return {
    name: field.name,
    type: field.type,
    required: Boolean(field.required),
    description: field.description ?? "",
    max_length: field.type === "string" && field.max_length != null ? field.max_length : null,
    values: field.type === "enum" ? (field.values ?? []).filter((v) => v.length > 0) : [],
  };
}

/** Add the sections to a spec, leaving each list out entirely while it is empty. */
export function withSpecSections<T extends AgentSpec>(spec: T, sections: SpecSections): T & Partial<SpecSections> {
  const {
    input_processors: _i,
    output_processors: _o,
    context_fields: _c,
    ...rest
  } = spec as T & Partial<SpecSections>;
  void _i; void _o; void _c;
  const out = { ...rest } as T & Partial<SpecSections>;
  if (sections.input_processors.length > 0) out.input_processors = sections.input_processors.map(normalizeProcessor);
  if (sections.output_processors.length > 0) out.output_processors = sections.output_processors.map(normalizeProcessor);
  if (sections.context_fields.length > 0) out.context_fields = sections.context_fields.map(normalizeContextField);
  return out;
}

function stableJson(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(stableJson).join(",")}]`;
  if (value && typeof value === "object") {
    const entries = Object.entries(value as Record<string, unknown>).filter(([, v]) => v !== undefined).sort(([a], [b]) => a.localeCompare(b));
    return `{${entries.map(([k, v]) => `${JSON.stringify(k)}:${stableJson(v)}`).join(",")}}`;
  }
  return JSON.stringify(value ?? null);
}

/** True when the edited sections differ from the stored ones (dirty-state tracking). */
export function sectionsDiffer(a: SpecSections, b: SpecSections): boolean {
  const norm = (s: SpecSections) => stableJson({
    i: s.input_processors.map(normalizeProcessor),
    o: s.output_processors.map(normalizeProcessor),
    c: s.context_fields.map(normalizeContextField),
  });
  return norm(a) !== norm(b);
}

// Mirrors eap_schemas.request_context: a well-formed placeholder, and anything that looks like one.
const PLACEHOLDER_RE = /\{\{\s*context\.([a-z][a-z0-9_]{0,31})\s*\}\}/g;
const PLACEHOLDER_FULL_RE = /^\{\{\s*context\.([a-z][a-z0-9_]{0,31})\s*\}\}$/;
const PLACEHOLDER_LIKE_RE = /\{\{\s*context\b[^{}]*\}\}/g;

/** Field names referenced by well-formed `{{context.<name>}}` placeholders, first-seen order. */
export function placeholders(text: string): string[] {
  const seen: string[] = [];
  for (const match of text.matchAll(PLACEHOLDER_RE)) {
    if (!seen.includes(match[1])) seen.push(match[1]);
  }
  return seen;
}

/** The publish check, client side: malformed placeholders and placeholders naming no field. */
export function placeholderErrors(text: string, fields: ContextField[]): string[] {
  const names = new Set(fields.map((f) => f.name));
  const errors: string[] = [];
  for (const match of text.matchAll(PLACEHOLDER_LIKE_RE)) {
    if (!PLACEHOLDER_FULL_RE.test(match[0])) errors.push(`malformed placeholder ${match[0].slice(0, 80)}: use {{context.<field name>}}`);
  }
  for (const name of placeholders(text)) {
    if (!names.has(name)) errors.push(`placeholder {{context.${name}}} names no context field`);
  }
  return errors;
}

/** Client-side problems with one context field (the server re-checks on save and publish). */
export function contextFieldErrors(field: ContextField, index: number, all: ContextField[]): string[] {
  const errors: string[] = [];
  if (!CONTEXT_NAME_PATTERN.test(field.name)) {
    errors.push("Name must start with a lowercase letter and use only a-z, 0-9 and _ (at most 32 characters)");
  } else if (RESERVED_CONTEXT_NAMES.includes(field.name)) {
    errors.push(`'${field.name}' is reserved: identity comes from the authenticated principal`);
  } else if (all.findIndex((other) => other.name === field.name) !== index) {
    errors.push(`Duplicate field name '${field.name}'`);
  }
  if (field.type === "enum") {
    const values = (field.values ?? []).filter((v) => v.length > 0);
    if (values.length === 0) errors.push("An enum field needs at least one value");
    else if (new Set(values).size !== values.length) errors.push("Enum values must be unique");
  }
  return errors;
}

/** A new processor's config: the schema defaults, with a moderation model when one is available. */
export function defaultProcessorConfig(declaration: ProcessorDeclaration, models: ModelOption[]): Record<string, unknown> {
  const config: Record<string, unknown> = {};
  for (const [key, schema] of Object.entries(declaration.config_schema.properties ?? {})) {
    if (schema.default !== undefined) config[key] = Array.isArray(schema.default) ? [...schema.default] : schema.default;
    else if (schema.type === "array") config[key] = [];
  }
  if (declaration.id === "moderation") {
    const model = models.find((m) => !m.wildcard && m.id === "test:moderation") ?? models.find((m) => !m.wildcard);
    config.model = model ? { provider: model.provider, name: model.name } : { provider: "", name: "" };
  }
  return config;
}

/**
 * Where a save or publish error belongs, from the API message prefix:
 * `input_processors[2]...` → that processor, `instructions:` → the instructions,
 * `processor model ...` → the processors section.
 */
export type ErrorTarget =
  | { kind: "processor"; phase: ProcessorPhase; index: number }
  | { kind: "processors" }
  | { kind: "instructions" }
  | { kind: "context_fields" }
  | null;

export function errorTarget(message: string): ErrorTarget {
  const proc = /^(input|output)_processors\[(\d+)\]/.exec(message);
  if (proc) return { kind: "processor", phase: proc[1] as ProcessorPhase, index: Number(proc[2]) };
  if (message.startsWith("instructions:")) return { kind: "instructions" };
  if (message.startsWith("processor model")) return { kind: "processors" };
  if (/context field|context_fields/.test(message)) return { kind: "context_fields" };
  return null;
}
