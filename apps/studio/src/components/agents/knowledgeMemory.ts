/**
 * Agent spec helpers for knowledge and memory (RAG and memory milestone, task RM.17): the
 * `knowledge_bases` and `memory` keys the editor's Knowledge and Memory steps write, their
 * client-side checks, status lines, server-error ownership, and the read-only summaries the Config
 * panel and the memory card show. Wire shapes and limits follow eap_schemas `knowledge.py`
 * (`KnowledgeBaseRef`, `validate_filter`) and `memory.py` (`MemoryConfig` and its sub-configs, the
 * working memory JSON Schema subset).
 *
 * Hash stability (the spec contract): both keys are left out of the spec while empty. Memory off
 * removes the `memory` key (the API treats a missing key and `null` alike and drops both from the
 * stored spec), never `{}`, which is "memory on with the default history limits". Inside, every
 * optional field and sub-config is left out while unset. Sub-configs the editor turns on are written
 * in the server's dump shape (field order, defaults explicit), so a saved and reloaded draft is the
 * same JSON as the one sent.
 *
 * While a value is being typed it may be invalid (a number out of range, JSON that does not parse);
 * the step keeps it in the spec as typed and `knowledgeProblems` / `memoryProblems` report it, which
 * blocks saving (the steps' `missing`) and marks the step in the nav.
 */
import type { AgentSpec } from "@/lib/api";

// ------------------------------------------------------------------ limits (eap_schemas mirrors)

export const MAX_KNOWLEDGE_BASES_PER_AGENT = 8;
export const KB_DEFAULT_TOP_K = 8;
export const KB_MAX_TOP_K = 20;
export const MAX_FILTER_KEYS = 16;
export const MAX_FILTER_LIST_VALUES = 32;
export const MAX_FILTER_STRING_LENGTH = 256;
export const METADATA_KEY_PATTERN = /^[A-Za-z0-9_.-]{1,64}$/;

export const HISTORY_DEFAULT_LAST_TURNS = 20;
export const HISTORY_MAX_LAST_TURNS = 200;
export const HISTORY_MAX_TOKENS = 1_000_000;
export const MAX_TEMPLATE_BYTES = 4 * 1024;
export const MAX_SCHEMA_BYTES = 8 * 1024;
export const MAX_SCHEMA_DEPTH = 3;
export const MAX_SCHEMA_PROPERTIES = 50;
export const MAX_SCHEMA_ENUM_VALUES = 64;
export const MAX_JSON_DEPTH = 16;
export const DEFAULT_WORKING_MEMORY_TEMPLATE = "# User\n- Name:\n- Preferences:\n- Goals:";
export const OBSERVATIONAL_DEFAULTS = { observe_after_tokens: 12_000, keep_recent_turns: 4, reflect_after_tokens: 8_000 } as const;
export const OBSERVE_TOKENS_RANGE = [1_000, 200_000] as const;
export const KEEP_RECENT_TURNS_RANGE = [0, 50] as const;
export const RECALL_DEFAULTS = { top_k: 4, message_range: 1 } as const;
export const RECALL_TOP_K_RANGE = [1, 10] as const;
export const RECALL_MESSAGE_RANGE = [0, 3] as const;
/**
 * eap_schemas.processors.PII_KINDS, in order: the fallback for the kinds the processor catalog
 * (`GET /api/v1/processors`, the pii_redact `kinds` enum) serves, and what the checks use.
 */
export const PII_KINDS = ["email", "phone", "credit_card", "iban", "ipv4", "us_ssn"] as const;
export type PiiKind = (typeof PII_KINDS)[number];

/**
 * The PII kinds the redaction checks accept: `PII_KINDS` until a processor catalog has been read
 * (`piiKindsFromCatalog`), then the catalog's list, which is the server's.
 */
let knownPiiKinds: readonly string[] = PII_KINDS;

export function acceptedPiiKinds(): readonly string[] {
  return knownPiiKinds;
}

/** Sets the accepted kinds (tests; `piiKindsFromCatalog` calls it). Null restores `PII_KINDS`. */
export function setAcceptedPiiKinds(kinds: readonly string[] | null): void {
  knownPiiKinds = kinds && kinds.length > 0 ? [...kinds] : PII_KINDS;
}

/**
 * The PII kinds from the processor catalog (the pii_redact `kinds` items enum), else `PII_KINDS`. A
 * catalog that lists them also becomes the kinds the redaction checks accept (`acceptedPiiKinds`),
 * so the Memory step, which loads the catalog, checks against the server's list.
 */
export function piiKindsFromCatalog(catalog: { items: { id: string; config_schema: unknown }[] } | null | undefined): string[] {
  const schema = catalog?.items.find((item) => item.id === "pii_redact")?.config_schema as
    | { properties?: { kinds?: { items?: { enum?: unknown } } } }
    | undefined;
  const values = schema?.properties?.kinds?.items?.enum;
  if (Array.isArray(values) && values.length > 0 && values.every((v) => typeof v === "string")) {
    setAcceptedPiiKinds(values as string[]);
    return [...(values as string[])];
  }
  return [...PII_KINDS];
}

/** The top-level spec keys of the Knowledge and Memory steps (422 `loc` routing). */
export const KNOWLEDGE_SPEC_KEYS = ["knowledge_bases"] as const;
export const MEMORY_SPEC_KEYS = ["memory"] as const;

/** A starter schema for "JSON schema" mode (inside the subset). */
export const STARTER_WORKING_MEMORY_SCHEMA = {
  type: "object",
  properties: {
    name: { type: "string" },
    preferences: { type: "array", items: { type: "string" } },
    goals: { type: "array", items: { type: "string" } },
  },
};

// ------------------------------------------------------------------ shapes

export type FilterScalar = string | number | boolean;
export type FilterValue = FilterScalar | FilterScalar[];

/**
 * One attached knowledge base (`KnowledgeBaseRef`). Fields may hold a value as typed while it is
 * edited (`unknown`): the checks report it.
 */
export interface KnowledgeBaseRefSpec {
  id: string;
  top_k?: number | unknown;
  min_score?: number | unknown;
  /** A filter object, or the text typed while it is not valid JSON. */
  filter?: Record<string, FilterValue> | unknown;
}

export interface ModelRefSpec {
  provider: string;
  name: string;
}

export interface HistorySpec {
  last_turns?: number | unknown;
  max_tokens?: number | unknown;
}

export type WorkingScope = "conversation" | "resource";
export interface WorkingSpec {
  scope?: WorkingScope;
  template?: string;
  /** A schema object, or the text typed while it is not valid JSON. */
  json_schema?: Record<string, unknown> | unknown;
  read_only?: boolean;
}

export interface ObservationalSpec {
  model: ModelRefSpec;
  observe_after_tokens?: number | unknown;
  keep_recent_turns?: number | unknown;
  reflect_after_tokens?: number | unknown;
}

export type RecallScope = "resource" | "conversation";
export interface SemanticRecallSpec {
  embedding_model: string;
  top_k?: number | unknown;
  message_range?: number | unknown;
  scope?: RecallScope;
  min_score?: number | unknown;
}

export interface RedactSpec {
  /** Unset: every kind (the API default). */
  kinds?: string[];
  action?: "redact" | unknown;
}

export interface MemorySpec {
  history?: HistorySpec;
  working?: WorkingSpec;
  observational?: ObservationalSpec;
  semantic_recall?: SemanticRecallSpec;
  redact?: RedactSpec;
}

export type AgentSpecWithKnowledgeMemory = AgentSpec & {
  knowledge_bases?: KnowledgeBaseRefSpec[];
  memory?: MemorySpec | null;
};

/** The spec's attached knowledge bases (none when absent). */
export function knowledgeBasesOf(spec: AgentSpec | null | undefined): KnowledgeBaseRefSpec[] {
  const list = (spec as AgentSpecWithKnowledgeMemory | null | undefined)?.knowledge_bases;
  return Array.isArray(list) ? list : [];
}

/** The spec's memory config, or null when memory is off. */
export function memoryOf(spec: AgentSpec | null | undefined): MemorySpec | null {
  const memory = (spec as AgentSpecWithKnowledgeMemory | null | undefined)?.memory;
  return memory && typeof memory === "object" && !Array.isArray(memory) ? memory : null;
}

// ------------------------------------------------------------------ building values

/**
 * A number field as typed: "" gives `fallback` (the API's default for a field that has one, so the
 * saved draft equals the API's dump of it; undefined for an optional field, which leaves the key
 * out), else the number, or the text when it is not one (the checks flag it).
 */
export function typedNumber(raw: string, fallback?: number): number | string | undefined {
  if (raw.trim() === "") return fallback;
  const value = Number(raw);
  return Number.isFinite(value) ? value : raw;
}

/**
 * `value` rebuilt the way the API dumps it: the `known` fields first in the model's order, each
 * filled from `defaults` when unset and left out when null or undefined (an optional field), then
 * any other keys as they were (the API refuses those, so they are kept for its message, never
 * silently dropped).
 */
function dumpShape<T extends object>(value: T, known: readonly string[], defaults: Record<string, unknown> = {}): T {
  const source = value as Record<string, unknown>;
  const out: Record<string, unknown> = {};
  for (const key of known) {
    const v = source[key] === undefined ? defaults[key] : source[key];
    if (v !== undefined && v !== null) out[key] = v;
  }
  for (const [key, v] of Object.entries(source)) if (!known.includes(key) && v !== undefined) out[key] = v;
  return out as T;
}

/** A new attachment in the API's dump shape. */
export function newKnowledgeBaseRef(id: string): KnowledgeBaseRefSpec {
  return { id, top_k: KB_DEFAULT_TOP_K };
}

/** An attachment in the API's dump shape (`top_k` defaulted; no or an empty filter left out). */
export function normalizeKnowledgeBaseRef(ref: KnowledgeBaseRefSpec): KnowledgeBaseRefSpec {
  const filter = isPlainObject(ref.filter) && Object.keys(ref.filter).length === 0 ? undefined : ref.filter;
  return dumpShape({ ...ref, filter }, ["id", "top_k", "min_score", "filter"], { top_k: KB_DEFAULT_TOP_K });
}

/** The `knowledge_bases` value to write: undefined (key removed) when the list is empty. */
export function knowledgeBasesValue(list: KnowledgeBaseRefSpec[]): KnowledgeBaseRefSpec[] | undefined {
  return list.length > 0 ? list.map(normalizeKnowledgeBaseRef) : undefined;
}

export const WORKING_DEFAULTS = { scope: "resource", read_only: false } as const;

export function newWorking(): WorkingSpec {
  return { ...WORKING_DEFAULTS };
}

export function normalizeWorking(working: WorkingSpec): WorkingSpec {
  return dumpShape(working, ["scope", "template", "json_schema", "read_only"], WORKING_DEFAULTS);
}

export function newObservational(model: ModelRefSpec | null): ObservationalSpec {
  return { model: model ?? { provider: "", name: "" }, ...OBSERVATIONAL_DEFAULTS };
}

export function normalizeObservational(value: ObservationalSpec): ObservationalSpec {
  return dumpShape(value, ["model", "observe_after_tokens", "keep_recent_turns", "reflect_after_tokens"], OBSERVATIONAL_DEFAULTS);
}

export function newSemanticRecall(embeddingModel: string | null): SemanticRecallSpec {
  return { embedding_model: embeddingModel ?? "", ...RECALL_DEFAULTS, scope: "resource" };
}

export function normalizeSemanticRecall(value: SemanticRecallSpec): SemanticRecallSpec {
  return dumpShape(value, ["embedding_model", "top_k", "message_range", "scope", "min_score"], { ...RECALL_DEFAULTS, scope: "resource" });
}

/** History in the API's dump shape: once there is a history config, `last_turns` is always dumped. */
export function normalizeHistory(value: HistorySpec): HistorySpec {
  return dumpShape(value, ["last_turns", "max_tokens"], { last_turns: HISTORY_DEFAULT_LAST_TURNS });
}

/** Redaction in the API's dump shape (`kinds` defaults to every kind, `action` to redact). */
export function normalizeRedact(value: RedactSpec): RedactSpec {
  return dumpShape(value, ["kinds", "action"], { kinds: [...PII_KINDS], action: "redact" });
}

/**
 * The `memory` value to write, in the API's dump shape: sub-configs in field order, unset ones left
 * out, set ones with their defaults filled in (so a saved draft equals the API's dump and is clean
 * after the save). `null` input (memory off) gives undefined, which removes the key; it never gives
 * `{}` for "off".
 */
export function memoryValue(memory: MemorySpec | null): MemorySpec | undefined {
  if (memory === null) return undefined;
  return dumpShape(
    {
      ...memory,
      history: memory.history ? normalizeHistory(memory.history) : undefined,
      working: memory.working ? normalizeWorking(memory.working) : undefined,
      observational: memory.observational ? normalizeObservational(memory.observational) : undefined,
      semantic_recall: memory.semantic_recall ? normalizeSemanticRecall(memory.semantic_recall) : undefined,
      redact: memory.redact ? normalizeRedact(memory.redact) : undefined,
    },
    ["history", "working", "observational", "semantic_recall", "redact"],
  );
}

// ------------------------------------------------------------------ checks

/** One problem: `field` names the input that shows it (an element id suffix), `message` is shown inline. */
export interface FieldProblem {
  field: string;
  message: string;
  /**
   * A warning rather than a blocker: it marks the step but blocks saving only once the user has
   * edited the step's key (a stored value the API may still accept, such as a PII kind this
   * Studio does not know).
   */
  soft?: boolean;
}

function isPlainObject(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/**
 * Python's `str.isspace()` set (and so `str.strip()` and `re`'s `\s` on str), which differs from
 * JavaScript's `\s`: it includes \x1c-\x1f and \x85, and excludes U+FEFF.
 */
const PY_SPACE = "\\t\\n\\v\\f\\r\\x1c-\\x20\\x85\\xa0\\u1680\\u2000-\\u200a\\u2028\\u2029\\u202f\\u205f\\u3000";
const PY_BLANK_RE = new RegExp(`^[${PY_SPACE}]*$`);

/** Python `not text.strip()`: empty or only Python whitespace. */
export function isPyBlank(text: string): boolean {
  return PY_BLANK_RE.test(text);
}

/** An unpaired UTF-16 surrogate: text Python cannot encode as UTF-8 (the API refuses it). */
export function hasLoneSurrogate(text: string): boolean {
  return /[\ud800-\udbff](?![\udc00-\udfff])|(?<![\ud800-\udbff])[\udc00-\udfff]/.test(text);
}

/** Length in code points (Python `len`), not UTF-16 units. */
function codePoints(text: string): number {
  return Array.from(text).length;
}

function utf8Length(text: string): number {
  return new TextEncoder().encode(text).length;
}

/** Python `json.dumps(..., sort_keys=True, separators=(",", ":"), ensure_ascii=False)`, for size checks. */
function compactSortedJson(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(compactSortedJson).join(",")}]`;
  if (isPlainObject(value)) {
    const keys = Object.keys(value).sort();
    return `{${keys.map((k) => `${JSON.stringify(k)}:${compactSortedJson(value[k])}`).join(",")}}`;
  }
  return JSON.stringify(value);
}

/** Problems with a whole-number field: `undefined` is fine (the default applies) unless `required`. */
function intProblem(value: unknown, label: string, min: number, max: number, required = false): string | null {
  if (value === undefined) return required ? `${label} is required` : null;
  if (typeof value !== "number" || !Number.isInteger(value) || value < min || value > max) {
    return `${label} must be a whole number from ${min.toLocaleString("en-US")} to ${max.toLocaleString("en-US")}`;
  }
  return null;
}

function scoreProblem(value: unknown, label: string): string | null {
  if (value === undefined || value === null) return null; // optional: null is "no minimum"
  if (typeof value !== "number" || !Number.isFinite(value) || value < 0 || value > 1) return `${label} must be a number from 0 to 1`;
  return null;
}

// Canonical form only. Knowledge base ids are never typed: they come from the list, or from a stored
// spec, which the API saves in canonical form (str(uuid.UUID(...))), so the other forms Python
// accepts (braces, urn:uuid:, no hyphens) cannot reach the editor.
const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

function filterScalarProblem(value: unknown, where: string): string | null {
  if (typeof value === "boolean") return null;
  // Python also refuses integers outside int64; a JS number cannot hold those exactly, so that
  // bound is left to the API.
  if (typeof value === "number") return Number.isFinite(value) ? null : `${where}: number must be finite`;
  if (typeof value === "string") {
    return codePoints(value) > MAX_FILTER_STRING_LENGTH ? `${where}: string longer than ${MAX_FILTER_STRING_LENGTH} characters` : null;
  }
  return `${where}: value must be a string, number or boolean`;
}

/**
 * The first problem with a metadata filter (`validate_filter`), or null. `value` is the parsed
 * filter, or the text typed while it does not parse.
 */
export function filterProblem(value: unknown): string | null {
  if (value === undefined || value === null) return null;
  if (typeof value === "string") return "Filter is not valid JSON";
  if (!isPlainObject(value)) return "Filter must be a JSON object";
  const entries = Object.entries(value);
  if (entries.length > MAX_FILTER_KEYS) return `Filter has more than ${MAX_FILTER_KEYS} keys`;
  for (const [key, item] of entries) {
    if (!METADATA_KEY_PATTERN.test(key)) return `Filter key '${key.slice(0, 64)}' must use only letters, digits, _ . - (at most 64 characters)`;
    if (Array.isArray(item)) {
      if (item.length < 1 || item.length > MAX_FILTER_LIST_VALUES) return `filter.${key}: a list needs 1 to ${MAX_FILTER_LIST_VALUES} values`;
      for (const v of item) {
        const problem = filterScalarProblem(v, `filter.${key}`);
        if (problem) return problem;
      }
    } else {
      const problem = filterScalarProblem(item, `filter.${key}`);
      if (problem) return problem;
    }
  }
  return null;
}

/** The problems with the attached knowledge bases (`KnowledgeBaseRef`, at most 8, unique). */
export function knowledgeProblems(spec: AgentSpec | null | undefined): FieldProblem[] {
  const refs = knowledgeBasesOf(spec);
  const problems: FieldProblem[] = [];
  if (refs.length > MAX_KNOWLEDGE_BASES_PER_AGENT) {
    problems.push({ field: "list", message: `At most ${MAX_KNOWLEDGE_BASES_PER_AGENT} knowledge bases per agent` });
  }
  const seen = new Set<string>();
  refs.forEach((ref, index) => {
    const id = typeof ref?.id === "string" ? ref.id : "";
    if (!UUID_RE.test(id)) problems.push({ field: `${index}-id`, message: "Knowledge base id must be a UUID" });
    else if (seen.has(id.toLowerCase())) problems.push({ field: `${index}-id`, message: "This knowledge base is attached twice" });
    seen.add(id.toLowerCase());
    const topK = intProblem(ref.top_k, "Top K", 1, KB_MAX_TOP_K);
    if (topK) problems.push({ field: `${index}-top_k`, message: topK });
    const minScore = scoreProblem(ref.min_score, "Minimum score");
    if (minScore) problems.push({ field: `${index}-min_score`, message: minScore });
    const filter = filterProblem(ref.filter);
    if (filter) problems.push({ field: `${index}-filter`, message: filter });
  });
  return problems;
}

// --- the working memory JSON Schema subset (eap_schemas.memory.validate_working_memory_schema) ---

const SCALAR_TYPES = ["string", "number", "integer", "boolean"];
const ALLOWED_KEYWORDS = new Set(["type", "properties", "items", "enum", "description", "title", "required", "additionalProperties"]);

class SchemaInvalid extends Error {
  constructor(
    readonly reason: string,
    readonly path = "",
  ) {
    super(path ? `${path}: ${reason}` : reason);
  }
}

function deeperThan(value: unknown, limit: number): boolean {
  const stack: [unknown, number][] = [[value, 1]];
  while (stack.length > 0) {
    const [node, depth] = stack.pop()!;
    const children = Array.isArray(node) ? node : isPlainObject(node) ? Object.values(node) : null;
    if (!children) continue;
    if (depth > limit) return true;
    for (const child of children) stack.push([child, depth + 1]);
  }
  return false;
}

function isScalar(value: unknown): boolean {
  return typeof value === "string" || typeof value === "boolean" || (typeof value === "number" && Number.isFinite(value));
}

// Integers above 2^53 cannot be told apart once parsed into JS numbers, so two distinct large enum
// values may be reported as duplicates here although Python accepts them; that edge is left as is.
function typedKey(value: unknown): string {
  return `${typeof value === "boolean" ? "bool" : typeof value === "number" ? "number" : "string"}:${String(value)}`;
}

function matchesType(value: unknown, type: string): boolean {
  if (type === "string") return typeof value === "string";
  if (type === "boolean") return typeof value === "boolean";
  if (type === "integer") return typeof value === "number" && Number.isInteger(value);
  if (type === "number") return typeof value === "number" && Number.isFinite(value);
  return false;
}

const join = (path: string, key: string) => (path ? `${path}.${key}` : key);
const quote = (value: unknown) => `'${String(value)}'`;

function checkNode(node: unknown, path: string, depth: number, count: { n: number }, inArray = false): void {
  const where = path || "(root)";
  if (!isPlainObject(node)) throw new SchemaInvalid("a schema must be a JSON object", where);
  const unknownKeys = Object.keys(node).filter((k) => !ALLOWED_KEYWORDS.has(k)).sort();
  if (unknownKeys.length > 0) throw new SchemaInvalid(`unsupported keyword ${quote(unknownKeys[0])}`, where);
  for (const key of ["description", "title"]) {
    if (key in node && typeof node[key] !== "string") throw new SchemaInvalid(`"${key}" must be a string`, where);
  }
  const type = node.type;
  const extraKeys = (allowed: string[]) => Object.keys(node).filter((k) => !allowed.includes(k)).sort();
  if ("enum" in node) {
    const values = node.enum;
    if (!Array.isArray(values) || values.length < 1 || values.length > MAX_SCHEMA_ENUM_VALUES) {
      throw new SchemaInvalid(`"enum" must be a list of 1 to ${MAX_SCHEMA_ENUM_VALUES} values`, where);
    }
    if (!values.every(isScalar)) throw new SchemaInvalid('"enum" values must be strings, numbers or booleans', where);
    if (new Set(values.map(typedKey)).size !== values.length) throw new SchemaInvalid('"enum" values must be unique', where);
    if (type !== undefined) {
      if (typeof type !== "string" || !SCALAR_TYPES.includes(type)) throw new SchemaInvalid('an "enum" may only have a scalar "type"', where);
      if (!values.every((v) => matchesType(v, type))) throw new SchemaInvalid(`"enum" values must all be of type ${type}`, where);
    }
    const extra = extraKeys(["enum", "type", "description", "title"]);
    if (extra.length > 0) throw new SchemaInvalid(`unsupported keyword ${quote(extra[0])} on an enum`, where);
    return;
  }
  if (typeof type === "string" && SCALAR_TYPES.includes(type)) {
    const extra = extraKeys(["type", "description", "title"]);
    if (extra.length > 0) throw new SchemaInvalid(`unsupported keyword ${quote(extra[0])} on a ${type}`, where);
    return;
  }
  if (inArray) throw new SchemaInvalid("array items must be a string, number, integer, boolean or enum", where);
  if (type === "array") {
    const extra = extraKeys(["type", "items", "description", "title"]);
    if (extra.length > 0) throw new SchemaInvalid(`unsupported keyword ${quote(extra[0])} on an array`, where);
    if (!("items" in node)) throw new SchemaInvalid('an array needs "items"', where);
    checkNode(node.items, `${path}[]`, depth, count, true);
    return;
  }
  if (type === "object") {
    if (depth > MAX_SCHEMA_DEPTH) throw new SchemaInvalid(`objects may be nested at most ${MAX_SCHEMA_DEPTH} deep`, where);
    if ("items" in node) throw new SchemaInvalid('unsupported keyword "items" on an object', where);
    // `"key" in node`, not `??`: an explicit null is refused, as in the API.
    const properties = "properties" in node ? node.properties : {};
    if (!isPlainObject(properties)) throw new SchemaInvalid('"properties" must be an object', where);
    const additional = "additionalProperties" in node ? node.additionalProperties : true;
    if (typeof additional !== "boolean") throw new SchemaInvalid('"additionalProperties" must be true or false', where);
    const required = "required" in node ? node.required : [];
    if (!Array.isArray(required) || !required.every((r) => typeof r === "string")) {
      throw new SchemaInvalid('"required" must be a list of property names', where);
    }
    for (const [name, child] of Object.entries(properties)) {
      if (!name || codePoints(name) > 64) throw new SchemaInvalid("property names must be 1 to 64 characters", where);
      count.n += 1;
      if (count.n > MAX_SCHEMA_PROPERTIES) throw new SchemaInvalid(`at most ${MAX_SCHEMA_PROPERTIES} properties in total`);
      checkNode(child, join(path, name), depth + 1, count);
    }
    return;
  }
  if (type === undefined) throw new SchemaInvalid('a schema needs a "type" or an "enum"', where);
  throw new SchemaInvalid(`unsupported type ${quote(type)}`, where);
}

/**
 * The first problem with a working memory schema (the API's JSON Schema subset), or null. `value`
 * is the parsed schema, or the text typed while it does not parse. Messages name the path, like
 * the API's ("profile.age: unsupported type 'date'").
 */
export function workingSchemaProblem(value: unknown): string | null {
  if (typeof value === "string") return isPyBlank(value) ? "Enter a JSON schema" : "Schema is not valid JSON";
  try {
    if (!isPlainObject(value)) throw new SchemaInvalid("the schema must be a JSON object");
    if (hasLoneSurrogate(compactSortedJson(value))) throw new SchemaInvalid("the schema must be JSON");
    if (deeperThan(value, MAX_JSON_DEPTH)) throw new SchemaInvalid(`the schema nests deeper than ${MAX_JSON_DEPTH} levels`);
    if (utf8Length(compactSortedJson(value)) > MAX_SCHEMA_BYTES) throw new SchemaInvalid(`the schema must be at most ${MAX_SCHEMA_BYTES} bytes`);
    if (value.type !== "object") throw new SchemaInvalid('the root must have "type": "object"');
    if ("$schema" in value && typeof value.$schema !== "string") throw new SchemaInvalid('"$schema" must be a string');
    const root = Object.fromEntries(Object.entries(value).filter(([k]) => k !== "$schema"));
    checkNode(root, "", 1, { n: 0 });
    return null;
  } catch (error) {
    if (error instanceof SchemaInvalid) return error.path ? error.message : error.message.charAt(0).toUpperCase() + error.message.slice(1);
    throw error;
  }
}

/** The first problem with a Markdown template, or null (`undefined`: the default template). */
export function templateProblem(value: unknown): string | null {
  if (value === undefined || value === null) return null;
  if (typeof value !== "string") return "Template must be text";
  if (hasLoneSurrogate(value)) return "Template contains a character that cannot be stored (an unpaired surrogate)";
  if (isPyBlank(value)) return "Template cannot be blank (leave it empty to use the default)";
  if (utf8Length(value) > MAX_TEMPLATE_BYTES) return `Template must be at most ${MAX_TEMPLATE_BYTES} bytes (it is ${utf8Length(value)})`;
  return null;
}

const EMBEDDING_PROVIDER_RE = /^[a-z][a-z0-9_-]{0,63}$/;
// Python `^[^\s,=]{1,128}$`: Python whitespace, 128 code points (the `u` flag).
const EMBEDDING_NAME_RE = new RegExp(`^[^${PY_SPACE},=]{1,128}$`, "u");

/** `parse_embedding_model_id`: "provider:name", the name not "*". */
export function embeddingModelIdProblem(id: unknown): string | null {
  if (typeof id !== "string" || id === "") return "Choose an embedding model";
  const sep = id.indexOf(":");
  const provider = sep < 0 ? "" : id.slice(0, sep);
  const name = sep < 0 ? "" : id.slice(sep + 1);
  if (sep < 0 || !EMBEDDING_PROVIDER_RE.test(provider)) return "Embedding model must be provider:name";
  if (!name || name === "*") return "Type an embedding model name";
  if (!EMBEDDING_NAME_RE.test(name)) return "Embedding model name may not contain spaces, commas or =";
  return null;
}

function modelProblem(model: unknown): string | null {
  const m = model as Partial<ModelRefSpec> | undefined;
  if (!m || typeof m.provider !== "string" || !m.provider) return "Choose an observer model";
  if (typeof m.name !== "string" || !m.name) return "Type a model name";
  if (codePoints(m.provider) > 128 || codePoints(m.name) > 128) return "Model provider and name must be at most 128 characters";
  return null;
}

/** Both scopes (working memory, semantic recall) take "resource" or "conversation"; unset is the default. */
function scopeProblem(scope: unknown): string | null {
  if (scope === undefined || scope === "resource" || scope === "conversation") return null;
  return "Scope must be per user (resource) or per conversation";
}

/** `PiiRedactConfig` with action redact: kinds (default: all) at least one, unique, and known to the catalog. */
function redactProblem(redact: unknown): string | null {
  const r = redact as { kinds?: unknown; action?: unknown };
  if (r.action !== undefined && r.action !== "redact") return "Memory redaction can only redact (memory writes are never blocked)";
  if (r.kinds === undefined) return null;
  if (!Array.isArray(r.kinds) || r.kinds.length === 0) return "Pick at least one kind of personal data";
  // Duplicates first: they block even beside an unknown kind (which is only a warning).
  if (new Set(r.kinds).size !== r.kinds.length) return "Each kind of personal data may be listed once";
  const accepted = acceptedPiiKinds();
  if (r.kinds.some((k) => !accepted.includes(k as string))) return `Unknown kind of personal data (use ${accepted.join(", ")})`;
  return null;
}

/** The problems with the memory config (`MemoryConfig`); none when memory is off. */
export function memoryProblems(spec: AgentSpec | null | undefined): FieldProblem[] {
  const memory = memoryOf(spec);
  if (!memory) return [];
  const problems: FieldProblem[] = [];
  const add = (field: string, message: string | null) => {
    if (message) problems.push({ field, message });
  };
  if (memory.history) {
    add("history-last_turns", intProblem(memory.history.last_turns, "Last turns", 1, HISTORY_MAX_LAST_TURNS));
    // Optional: null is "no token budget", as in the API.
    if (memory.history.max_tokens !== null) add("history-max_tokens", intProblem(memory.history.max_tokens, "Max tokens", 1, HISTORY_MAX_TOKENS));
  }
  if (memory.working) {
    const working = memory.working;
    add("working-scope", scopeProblem(working.scope));
    if (working.template != null && working.json_schema != null) add("working-format", "Set either a template or a JSON schema, not both");
    if (working.json_schema != null) add("working-schema", workingSchemaProblem(working.json_schema));
    else add("working-template", templateProblem(working.template));
  }
  if (memory.observational) {
    const o = memory.observational;
    add("observational-model", modelProblem(o.model));
    add("observational-observe_after_tokens", intProblem(o.observe_after_tokens, "Observe after", ...OBSERVE_TOKENS_RANGE));
    add("observational-keep_recent_turns", intProblem(o.keep_recent_turns, "Recent turns kept", ...KEEP_RECENT_TURNS_RANGE));
    add("observational-reflect_after_tokens", intProblem(o.reflect_after_tokens, "Reflect after", ...OBSERVE_TOKENS_RANGE));
  }
  if (memory.semantic_recall) {
    const r = memory.semantic_recall;
    add("recall-embedding_model", embeddingModelIdProblem(r.embedding_model));
    add("recall-top_k", intProblem(r.top_k, "Top K", ...RECALL_TOP_K_RANGE));
    add("recall-message_range", intProblem(r.message_range, "Message range", ...RECALL_MESSAGE_RANGE));
    add("recall-min_score", scoreProblem(r.min_score, "Minimum score"));
    add("recall-scope", scopeProblem(r.scope));
  }
  if (memory.redact) {
    const problem = redactProblem(memory.redact);
    if (problem) problems.push({ field: "redact-kinds", message: problem, soft: problem.startsWith("Unknown kind") || undefined });
  }
  return problems;
}

// ------------------------------------------------------------------ status lines

export interface KnowledgeMemoryStatus {
  text: string;
  valid: boolean;
}

function plural(count: number, one: string, many = `${one}s`): string {
  return `${count} ${count === 1 ? one : many}`;
}

/** The Knowledge step's nav status: "None", "2 knowledge bases", or "1 problem". */
export function knowledgeStatus(spec: AgentSpec | null | undefined): KnowledgeMemoryStatus {
  const problems = knowledgeProblems(spec).length;
  if (problems > 0) return { text: plural(problems, "problem"), valid: false };
  const count = knowledgeBasesOf(spec).length;
  return { text: count === 0 ? "None" : plural(count, "knowledge base"), valid: true };
}

/** The history window in force: the configured turns, the default with memory on, null (all turns) with memory off. */
export function effectiveLastTurns(memory: MemorySpec | null): number | null {
  if (!memory) return null;
  const turns = memory.history?.last_turns;
  return typeof turns === "number" ? turns : HISTORY_DEFAULT_LAST_TURNS;
}

/** The Memory step's nav status: "Off", "History 20 · Working · Recall", or "1 problem". */
export function memoryStatus(spec: AgentSpec | null | undefined): KnowledgeMemoryStatus {
  const problems = memoryProblems(spec).length;
  if (problems > 0) return { text: plural(problems, "problem"), valid: false };
  const memory = memoryOf(spec);
  if (!memory) return { text: "Off", valid: true };
  const parts = [`History ${effectiveLastTurns(memory)}`];
  if (memory.working) parts.push("Working");
  if (memory.observational) parts.push("Observational");
  if (memory.semantic_recall) parts.push("Recall");
  return { text: parts.join(" · "), valid: true };
}

// ------------------------------------------------------------------ server errors

/**
 * Whether a save or publish error is about `knowledge_bases`: the publish check
 * ("knowledge base '<id>' does not exist in this tenant") and the spec validators' messages
 * (UUID, at most 8, duplicates, the metadata filter).
 */
export function isKnowledgeError(message: string): boolean {
  return /knowledge[ _]base|(^|, |error, )filter([.:]| must| has)|metadata key must match/i.test(message);
}

/**
 * Whether a save or publish error is about `memory`: "memory.semantic_recall: ...", "observer model
 * '...' is not allowed", the embedding allowlist, and the memory validators' messages (template,
 * schema, redaction).
 */
export function isMemoryError(message: string): boolean {
  // Messages a 422 carries without a location (publish checks), or when its `loc` is missing; a
  // save's 422 is routed by `loc` first (`specKeys`). "duplicate PII kind" is memory.redact's own
  // (a pii_redact processor's kinds are validated the same way, but its 422 carries a loc).
  return /(^|[\s,])memory[.:\s]|observer model|embedding model|working memory|(^|, |error, )template |template or json_schema|(^|[\s,'"])(memory\.)?working(_memory)?\.json_schema|EAP_ALLOWED_EMBEDDING_MODELS|duplicate PII kind/i.test(message);
}

// ------------------------------------------------------------------ summaries (Config panel, memory card)

/** A memory feature in the memory card and the Config panel. */
export interface MemoryFeature {
  id: "history" | "working" | "observational" | "semantic";
  label: string;
  on: boolean;
}

/**
 * The four memory features of a spec (the memory card's badges). History is always on: without
 * memory a conversation replays all its turns ("History"); with memory, the window in force
 * ("History 20 turns", the default when `history` is unset).
 */
export function memoryFeatures(spec: AgentSpec | null | undefined): MemoryFeature[] {
  const memory = memoryOf(spec);
  const turns = effectiveLastTurns(memory);
  return [
    { id: "history", label: turns === null ? "History" : `History ${turns} turns`, on: true },
    { id: "working", label: "Working memory", on: Boolean(memory?.working) },
    { id: "observational", label: "Observational", on: Boolean(memory?.observational) },
    { id: "semantic", label: "Semantic recall", on: Boolean(memory?.semantic_recall) },
  ];
}

const formatNumber = (value: unknown) => (typeof value === "number" ? value.toLocaleString("en-US") : String(value));

/** One line per attachment setting: "Top 8 · min score 0.5 · filter lang=en, tag=faq|guide". */
export function knowledgeRefSummary(ref: KnowledgeBaseRefSpec): string {
  const parts = [`Top ${formatNumber(ref.top_k ?? KB_DEFAULT_TOP_K)}`];
  if (ref.min_score != null) parts.push(`min score ${formatNumber(ref.min_score)}`);
  if (isPlainObject(ref.filter) && Object.keys(ref.filter).length > 0) {
    const filter = Object.entries(ref.filter)
      .map(([key, value]) => `${key}=${Array.isArray(value) ? value.map(String).join("|") : String(value)}`)
      .join(", ");
    parts.push(`filter ${filter}`);
  }
  return parts.join(" · ");
}

/** Read-only lines for the Config panel's Memory section ([label, detail]); empty when memory is off. */
export function memorySummaryLines(spec: AgentSpec | null | undefined): [string, string][] {
  const memory = memoryOf(spec);
  if (!memory) return [];
  const lines: [string, string][] = [];
  const history = [`Last ${effectiveLastTurns(memory)} turns`];
  if (typeof memory.history?.max_tokens === "number") history.push(`at most ${formatNumber(memory.history.max_tokens)} tokens`);
  lines.push(["History", history.join(", ")]);
  if (memory.working) {
    const w = memory.working;
    const shape = w.json_schema != null ? "JSON schema" : w.template != null ? "Markdown template" : "Default template";
    const scope = (w.scope ?? "resource") === "resource" ? "per user" : "per conversation";
    lines.push(["Working memory", [shape, scope, w.read_only ? "read-only" : null].filter(Boolean).join(", ")]);
  }
  if (memory.observational) {
    const o = memory.observational;
    lines.push([
      "Observational",
      `${o.model?.provider ?? ""}:${o.model?.name ?? ""}, observe after ${formatNumber(o.observe_after_tokens ?? OBSERVATIONAL_DEFAULTS.observe_after_tokens)} tokens`,
    ]);
  }
  if (memory.semantic_recall) {
    const r = memory.semantic_recall;
    const scope = (r.scope ?? "resource") === "resource" ? "same user" : "same conversation";
    lines.push(["Semantic recall", `${r.embedding_model}, top ${formatNumber(r.top_k ?? RECALL_DEFAULTS.top_k)}, ${scope}`]);
  }
  lines.push(["Redaction", memory.redact ? (memory.redact.kinds ?? PII_KINDS).join(", ") : "Same as input redaction"]);
  return lines;
}

/** The kinds memory inherits from the input `pii_redact` processors (`MemoryConfig.effective_redact`). */
export function inheritedRedactKinds(inputProcessors: readonly { type: string; config: Record<string, unknown> }[]): string[] {
  const kinds: string[] = [];
  for (const processor of inputProcessors) {
    if (processor.type !== "pii_redact") continue;
    const list = processor.config.kinds;
    const processorKinds = Array.isArray(list) ? (list as string[]) : [...PII_KINDS];
    for (const kind of processorKinds) if (!kinds.includes(kind)) kinds.push(kind);
  }
  return kinds;
}
