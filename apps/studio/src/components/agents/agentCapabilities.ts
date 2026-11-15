/**
 * Agent capabilities in the editor (agent capabilities milestone, AC.13): structured output
 * (`spec.output`), tool approvals (`spec.approvals`), usage limits (`spec.limits`) and fallback models
 * (`spec.fallback_models`). Pure helpers only; the steps that use them are `OutputStep.tsx`,
 * `ToolsStep.tsx` (with `ApprovalToggles.tsx`) and `IdentityStep.tsx` (with `LimitsFields.tsx`).
 *
 * Wire shapes follow eap_schemas `output.py`, `approvals.py`, `limits.py` and `AgentSpec`. Every one
 * of the four keys is left out of the spec while unset, so agents without them keep their exact JSON
 * and `spec_hash`. The client checks mirror the server's (the API re-checks on save and publish).
 */
import type { AgentSpec, McpToolRef } from "@/lib/api";

// ------------------------------------------------------------------ contracts

export type OutputMode = "tool" | "native" | "prompted";
export const OUTPUT_MODES: readonly OutputMode[] = ["tool", "native", "prompted"];
export const DEFAULT_OUTPUT_MODE: OutputMode = "tool";
export const DEFAULT_OUTPUT_MAX_RETRIES = 2;
export const MAX_OUTPUT_RETRIES = 5;
export const MAX_OUTPUT_DESCRIPTION_CHARS = 1000;
export const OUTPUT_NAME_PATTERN = /^[A-Za-z0-9_-]{1,64}$/;

/** `spec.output` (fields the user left unset are absent). `json_schema` is any JSON while edited. */
export interface OutputSpecValue {
  json_schema: unknown;
  mode?: OutputMode | null;
  name?: string | null;
  description?: string | null;
  max_retries?: unknown;
}

export const DEFAULT_APPROVAL_TIMEOUT_HOURS = 24;
export const MAX_APPROVAL_TIMEOUT_HOURS = 168;
export const MAX_APPROVAL_TOOLS = 64;

/** `spec.approvals`: built-in tool ids and `"<server>/<tool>"` MCP refs that need a person's approval. */
export interface ApprovalPolicyValue {
  tools: string[];
  timeout_hours?: unknown;
}

export const MAX_REQUESTS = 200;
export const MAX_TOOL_CALLS = 500;
export const MAX_TOKENS = 10_000_000;
export const MIN_COST_USD = 0.0001;
export const MAX_COST_USD = 1000;

/** `spec.limits`. `max_cost_usd` is a decimal string ("0.5", as the API dumps it) or a number. */
export interface LimitsValue {
  max_requests?: unknown;
  max_tool_calls?: unknown;
  max_input_tokens?: unknown;
  max_output_tokens?: unknown;
  max_total_tokens?: unknown;
  max_cost_usd?: unknown;
}

export const LIMIT_FIELDS: readonly { key: Exclude<keyof LimitsValue, "max_cost_usd">; label: string; max: number }[] = [
  { key: "max_requests", label: "Model requests", max: MAX_REQUESTS },
  { key: "max_tool_calls", label: "Tool calls", max: MAX_TOOL_CALLS },
  { key: "max_input_tokens", label: "Input tokens", max: MAX_TOKENS },
  { key: "max_output_tokens", label: "Output tokens", max: MAX_TOKENS },
  { key: "max_total_tokens", label: "Total tokens", max: MAX_TOKENS },
];

export const MAX_FALLBACK_MODELS = 3;

export interface ModelRefValue {
  provider: string;
  name: string;
}

/** The top-level spec keys of this milestone, by the step that owns them. */
export const OUTPUT_SPEC_KEYS = ["output"] as const;
export const IDENTITY_CAPABILITY_KEYS = ["limits", "fallback_models"] as const;
export const TOOLS_CAPABILITY_KEYS = ["approvals"] as const;

type SpecWithCapabilities = AgentSpec & {
  output?: OutputSpecValue | null;
  approvals?: ApprovalPolicyValue | null;
  limits?: LimitsValue | null;
  fallback_models?: ModelRefValue[];
};

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

export function outputOf(spec: AgentSpec | null | undefined): OutputSpecValue | null {
  const value = (spec as SpecWithCapabilities | null | undefined)?.output;
  return isRecord(value) ? (value as unknown as OutputSpecValue) : null;
}

export function approvalsOf(spec: AgentSpec | null | undefined): ApprovalPolicyValue | null {
  const value = (spec as SpecWithCapabilities | null | undefined)?.approvals;
  return isRecord(value) && Array.isArray(value.tools) ? (value as unknown as ApprovalPolicyValue) : null;
}

export function limitsOf(spec: AgentSpec | null | undefined): LimitsValue | null {
  const value = (spec as SpecWithCapabilities | null | undefined)?.limits;
  return isRecord(value) ? (value as LimitsValue) : null;
}

export function fallbackModelsOf(spec: AgentSpec | null | undefined): ModelRefValue[] {
  const value = (spec as SpecWithCapabilities | null | undefined)?.fallback_models;
  return Array.isArray(value) ? value : [];
}

/** The output schema's top-level property names (empty when the schema is not an object). */
export function outputFieldNames(output: OutputSpecValue | null): string[] {
  const schema = output?.json_schema;
  return isRecord(schema) && isRecord(schema.properties) ? Object.keys(schema.properties) : [];
}

export function effectiveOutputMode(output: OutputSpecValue | null): OutputMode {
  return output?.mode && OUTPUT_MODES.includes(output.mode) ? output.mode : DEFAULT_OUTPUT_MODE;
}

/** A schema node's type for display ("string", "array of object", "string or null"). */
export function schemaTypeLabel(node: unknown): string {
  if (!isRecord(node)) return "?";
  if (Array.isArray(node.enum)) return "enum";
  if ("const" in node) return "const";
  const raw = node.type;
  const base = Array.isArray(raw) ? raw.find((t) => t !== "null") : raw;
  const nullable = Array.isArray(raw) && raw.includes("null");
  let label = typeof base === "string" ? base : "?";
  if (base === "array") label = `array of ${schemaTypeLabel(node.items)}`;
  return nullable ? `${label} or null` : label;
}

// ------------------------------------------------------------------ the JSON Schema subset (client mirror)

// eap_schemas.json_schema_subset, the "standard" dialect with the structured-output limits.
export const OUTPUT_SCHEMA_MAX_DEPTH = 5;
export const OUTPUT_SCHEMA_MAX_PROPERTIES = 100;
export const OUTPUT_SCHEMA_MAX_BYTES = 16 * 1024;
export const OUTPUT_SCHEMA_MAX_ENUM_VALUES = 256;
const MAX_JSON_DEPTH = 16;
const DRAFT_2020_12 = "https://json-schema.org/draft/2020-12/schema";
export const PROPERTY_NAME_PATTERN = /^[A-Za-z0-9_-]{1,64}$/;

const SCALAR_TYPES = ["string", "number", "integer", "boolean"];
const ALL_TYPES = [...SCALAR_TYPES, "object", "array", "null"];
const KEYWORDS = new Set([
  "type", "properties", "items", "enum", "const", "description", "title", "required",
  "additionalProperties", "minimum", "maximum", "minLength", "maxLength", "minItems", "maxItems", "format",
]);
const DESCRIPTIVE = ["description", "title"];
const STRING_EXTRA = ["minLength", "maxLength", "format"];
const NUMBER_EXTRA = ["minimum", "maximum"];
const ARRAY_EXTRA = ["minItems", "maxItems"];

/** A schema outside the subset: the path (empty at the root) and the rule. Never a value. */
export class SchemaProblem extends Error {
  constructor(public reason: string, public path = "") {
    super(`the JSON Schema is invalid${path ? ` at ${path}` : ""}: ${reason}`);
    this.name = "SchemaProblem";
  }
}

function jsonDepthExceeds(value: unknown, limit: number): boolean {
  const stack: [unknown, number][] = [[value, 1]];
  while (stack.length > 0) {
    const [node, depth] = stack.pop()!;
    const children = Array.isArray(node) ? node : isRecord(node) ? Object.values(node) : null;
    if (!children) continue;
    if (depth > limit) return true;
    for (const child of children) stack.push([child, depth + 1]);
  }
  return false;
}

/** Canonical JSON (sorted keys, compact), as the server measures a schema's size. */
function canonicalJson(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(canonicalJson).join(",")}]`;
  if (isRecord(value)) {
    const entries = Object.entries(value).filter(([, v]) => v !== undefined).sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0));
    return `{${entries.map(([k, v]) => `${JSON.stringify(k)}:${canonicalJson(v)}`).join(",")}}`;
  }
  return JSON.stringify(value ?? null);
}

/** UTF-8 length of the canonical JSON (sorted keys, compact). */
export function jsonBytes(value: unknown): number {
  return new TextEncoder().encode(canonicalJson(value)).length;
}

const isScalar = (v: unknown) => typeof v === "string" || typeof v === "boolean" || (typeof v === "number" && Number.isFinite(v));
const isNumber = (v: unknown) => typeof v === "number" && Number.isFinite(v);
const isCount = (v: unknown) => typeof v === "number" && Number.isInteger(v) && v >= 0;

function matchesType(value: unknown, type: string): boolean {
  switch (type) {
    case "null":
      return value === null;
    case "string":
      return typeof value === "string";
    case "boolean":
      return typeof value === "boolean";
    case "integer":
      return typeof value === "number" && Number.isInteger(value);
    case "number":
      return typeof value === "number" && Number.isFinite(value);
    default:
      return false;
  }
}

class Walker {
  count = 0;

  node(node: unknown, path: string, depth: number): void {
    const where = path || "(root)";
    if (!isRecord(node)) throw new SchemaProblem("a schema must be a JSON object", where);
    const unknown = Object.keys(node).filter((k) => !KEYWORDS.has(k)).sort();
    if (unknown.length > 0) throw new SchemaProblem(`unsupported keyword '${unknown[0]}'`, where);
    for (const key of DESCRIPTIVE) if (key in node && typeof node[key] !== "string") throw new SchemaProblem(`"${key}" must be a string`, where);
    const [type, nullable] = this.type(node.type, where);
    if ("enum" in node) return this.enumNode(node, type, nullable, where);
    if ("const" in node) return this.constNode(node, type, nullable, where);
    if ((type && SCALAR_TYPES.includes(type)) || type === "null") {
      const allowed = new Set(["type", ...DESCRIPTIVE, ...(type === "string" ? STRING_EXTRA : type === "number" || type === "integer" ? NUMBER_EXTRA : [])]);
      const extra = Object.keys(node).filter((k) => !allowed.has(k)).sort();
      if (extra.length > 0) throw new SchemaProblem(`unsupported keyword '${extra[0]}' on a ${type}`, where);
      return this.bounds(node, where);
    }
    if (type === "array") {
      const allowed = new Set(["type", "items", ...DESCRIPTIVE, ...ARRAY_EXTRA]);
      const extra = Object.keys(node).filter((k) => !allowed.has(k)).sort();
      if (extra.length > 0) throw new SchemaProblem(`unsupported keyword '${extra[0]}' on an array`, where);
      if (!("items" in node)) throw new SchemaProblem('an array needs "items"', where);
      this.bounds(node, where);
      return this.node(node.items, `${path}[]`, depth);
    }
    if (type === "object") return this.object(node, path, where, depth);
    if (type === null) throw new SchemaProblem('a schema needs a "type", an "enum" or a "const"', where);
    throw new SchemaProblem("unsupported type", where);
  }

  private type(raw: unknown, where: string): [string | null, boolean] {
    const message = '"type" must be one type or a list of one type and "null"';
    if (Array.isArray(raw)) {
      if (raw.length !== 2 || !raw.includes("null") || !raw.every((t) => typeof t === "string") || raw[0] === raw[1] || raw.some((t) => !ALL_TYPES.includes(t))) {
        throw new SchemaProblem(message, where);
      }
      return [raw.find((t) => t !== "null") as string, true];
    }
    if (raw !== undefined && raw !== null && typeof raw !== "string") throw new SchemaProblem(message, where);
    const type = typeof raw === "string" ? raw : null;
    return [type, type === "null"];
  }

  private valueMatches(value: unknown, type: string, nullable: boolean): boolean {
    return value === null ? nullable : matchesType(value, type);
  }

  private enumNode(node: Record<string, unknown>, type: string | null, nullable: boolean, where: string) {
    const values = node.enum;
    if (!Array.isArray(values) || values.length < 1 || values.length > OUTPUT_SCHEMA_MAX_ENUM_VALUES) {
      throw new SchemaProblem(`"enum" must be a list of 1 to ${OUTPUT_SCHEMA_MAX_ENUM_VALUES} values`, where);
    }
    if (!values.every((v) => isScalar(v) || v === null)) throw new SchemaProblem('"enum" values must be strings, numbers, booleans or null', where);
    if (new Set(values.map((v) => `${v === null ? "null" : typeof v}:${String(v)}`)).size !== values.length) throw new SchemaProblem('"enum" values must be unique', where);
    if (type !== null) {
      if (!SCALAR_TYPES.includes(type) && type !== "null") throw new SchemaProblem('an "enum" may only have a scalar "type"', where);
      if (!values.every((v) => this.valueMatches(v, type, nullable))) throw new SchemaProblem(`"enum" values must all be of type ${type}`, where);
    }
    const extra = Object.keys(node).filter((k) => !["enum", "type", ...DESCRIPTIVE].includes(k)).sort();
    if (extra.length > 0) throw new SchemaProblem(`unsupported keyword '${extra[0]}' on an enum`, where);
  }

  private constNode(node: Record<string, unknown>, type: string | null, nullable: boolean, where: string) {
    const value = node.const;
    if (!(isScalar(value) || value === null)) throw new SchemaProblem('"const" must be a string, number, boolean or null', where);
    if (type !== null) {
      if (!SCALAR_TYPES.includes(type) && type !== "null") throw new SchemaProblem('a "const" may only have a scalar "type"', where);
      if (!this.valueMatches(value, type, nullable)) throw new SchemaProblem(`"const" must be of type ${type}`, where);
    }
    const extra = Object.keys(node).filter((k) => !["const", "type", ...DESCRIPTIVE].includes(k)).sort();
    if (extra.length > 0) throw new SchemaProblem(`unsupported keyword '${extra[0]}' on a const`, where);
  }

  private bounds(node: Record<string, unknown>, where: string) {
    const pairs: [string, string, (v: unknown) => boolean, string][] = [
      ["minimum", "maximum", isNumber, "a finite number"],
      ["minLength", "maxLength", isCount, "a non-negative integer"],
      ["minItems", "maxItems", isCount, "a non-negative integer"],
    ];
    for (const [low, high, check, what] of pairs) {
      for (const key of [low, high]) if (key in node && !check(node[key])) throw new SchemaProblem(`"${key}" must be ${what}`, where);
      if (low in node && high in node && (node[low] as number) > (node[high] as number)) throw new SchemaProblem(`"${low}" must not be greater than "${high}"`, where);
    }
    if ("format" in node && typeof node.format !== "string") throw new SchemaProblem('"format" must be a string', where);
  }

  private object(node: Record<string, unknown>, path: string, where: string, depth: number) {
    if (depth > OUTPUT_SCHEMA_MAX_DEPTH) throw new SchemaProblem(`objects may be nested at most ${OUTPUT_SCHEMA_MAX_DEPTH} deep`, where);
    const extra = Object.keys(node).filter((k) => !["type", "properties", "required", "additionalProperties", ...DESCRIPTIVE].includes(k)).sort();
    // The server words this one with double quotes (the others use repr: single quotes).
    if (extra.length > 0) throw new SchemaProblem(`unsupported keyword "${extra[0]}" on an object`, where);
    const properties = node.properties ?? {};
    if (!isRecord(properties)) throw new SchemaProblem('"properties" must be an object', where);
    if ("additionalProperties" in node && typeof node.additionalProperties !== "boolean") throw new SchemaProblem('"additionalProperties" must be true or false', where);
    const required = node.required ?? [];
    if (!Array.isArray(required) || !required.every((r) => typeof r === "string")) throw new SchemaProblem('"required" must be a list of property names', where);
    if (new Set(required).size !== required.length) throw new SchemaProblem('"required" names must be unique', where);
    if (required.some((r) => !(r in properties))) throw new SchemaProblem('"required" may only name properties declared in "properties"', where);
    for (const [name, child] of Object.entries(properties)) {
      if (!PROPERTY_NAME_PATTERN.test(name)) throw new SchemaProblem("property names must match ^[A-Za-z0-9_-]{1,64}$", where);
      this.count += 1;
      if (this.count > OUTPUT_SCHEMA_MAX_PROPERTIES) throw new SchemaProblem(`at most ${OUTPUT_SCHEMA_MAX_PROPERTIES} properties in total`);
      this.node(child, path ? `${path}.${name}` : name, depth + 1);
    }
  }
}

/**
 * The first reason `schema` is outside the structured-output subset (as the API words it), or null
 * when it is inside. A string is JSON text that did not parse.
 */
export function outputSchemaProblem(schema: unknown): string | null {
  if (schema === undefined || schema === null) return "the JSON Schema is required";
  if (typeof schema === "string") return "the JSON Schema is not valid JSON";
  try {
    if (!isRecord(schema)) throw new SchemaProblem("the schema must be a JSON object");
    if (jsonDepthExceeds(schema, MAX_JSON_DEPTH)) throw new SchemaProblem(`the schema nests deeper than ${MAX_JSON_DEPTH} levels`);
    if (jsonBytes(schema) > OUTPUT_SCHEMA_MAX_BYTES) throw new SchemaProblem(`the schema must be at most ${OUTPUT_SCHEMA_MAX_BYTES} bytes`);
    if (schema.type !== "object") throw new SchemaProblem('the root must have "type": "object"');
    if ("$schema" in schema && typeof schema.$schema !== "string") throw new SchemaProblem('"$schema" must be a string');
    if ("$schema" in schema && schema.$schema !== DRAFT_2020_12) throw new SchemaProblem(`"$schema" may only be ${DRAFT_2020_12}`);
    const { $schema: _s, ...root } = schema;
    void _s;
    new Walker().node(root, "", 1);
    return null;
  } catch (error) {
    if (error instanceof SchemaProblem) return error.message;
    throw error;
  }
}

// ------------------------------------------------------------------ the field builder model

export type BuilderType = "string" | "number" | "integer" | "boolean" | "object" | "array";
export const BUILDER_TYPES: readonly BuilderType[] = ["string", "number", "integer", "boolean", "object", "array"];
export type BuilderItemType = Exclude<BuilderType, "array">;

/**
 * One row of the field builder. `rest` and `itemRest` keep the node's (and an array's items') other
 * keywords as they are (`minimum`, `format`, `title`, an items `enum`...), so a schema the builder
 * can show round-trips through it unchanged. `key` is an editor-only id.
 */
export interface BuilderField {
  key: string;
  name: string;
  type: BuilderType;
  nullable: boolean;
  required: boolean;
  description: string;
  /** String fields only: the allowed values, or null for any string. */
  enumValues: string[] | null;
  /** Arrays only: the item type. */
  itemType: BuilderItemType;
  /** Object properties; for an array of objects, the item's properties. */
  children: BuilderField[];
  rest: Record<string, unknown>;
  itemRest: Record<string, unknown>;
  /** The object (or array item object) had a `required` list, even an empty one: kept on output. */
  keepRequired: boolean;
  /**
   * The node as it was loaded, and the row's signature then (`nodeSignature`). While the row is
   * unchanged the builder writes the loaded node back as is (its key order and keywords), so an edit
   * to one row never rewrites its siblings.
   */
  origin?: { node: unknown; signature: string };
}

/** The root of the builder: its fields plus the root object's other keywords. */
export interface BuilderRoot {
  fields: BuilderField[];
  rest: Record<string, unknown>;
  keepRequired: boolean;
}

let builderKeyCounter = 0;
export function newBuilderKey(): string {
  builderKeyCounter += 1;
  return `f${builderKeyCounter}`;
}

export function newBuilderField(name = ""): BuilderField {
  return {
    key: newBuilderKey(),
    name,
    type: "string",
    nullable: false,
    required: true,
    description: "",
    enumValues: null,
    itemType: "string",
    children: [],
    rest: {},
    itemRest: {},
    keepRequired: false,
  };
}

class NotRepresentable extends Error {}

function splitType(raw: unknown): [string, boolean] {
  if (Array.isArray(raw)) return [raw.find((t) => t !== "null") as string, true];
  if (typeof raw !== "string") throw new NotRepresentable();
  return [raw, false];
}

function objectFields(node: Record<string, unknown>): { fields: BuilderField[]; rest: Record<string, unknown>; keepRequired: boolean } {
  const { type: _t, properties, required, ...rest } = node;
  void _t;
  if (properties !== undefined && !isRecord(properties)) throw new NotRepresentable();
  const req = Array.isArray(required) ? (required as string[]) : [];
  const fields = Object.entries(properties ?? {}).map(([name, child]) => fieldFromNode(name, child, req.includes(name)));
  return { fields, rest, keepRequired: required !== undefined };
}

/**
 * What a row's own node is made of (not its name or `required`, which live on the parent), with its
 * children's names and `required` included: equal signatures write equal nodes.
 */
function nodeSignature(field: BuilderField): string {
  const state = (f: BuilderField): unknown => ({
    type: f.type,
    nullable: f.nullable,
    description: f.description,
    enumValues: f.enumValues,
    itemType: f.itemType,
    rest: f.rest,
    itemRest: f.itemRest,
    keepRequired: f.keepRequired,
    children: f.children.map((child) => ({ name: child.name, required: child.required, node: state(child) })),
  });
  return canonicalJson(state(field));
}

function fieldFromNode(name: string, node: unknown, required: boolean): BuilderField {
  const field = loadField(name, node, required);
  field.origin = { node, signature: nodeSignature(field) };
  return field;
}

function loadField(name: string, node: unknown, required: boolean): BuilderField {
  if (!isRecord(node)) throw new NotRepresentable();
  const [type, nullable] = splitType(node.type);
  if (!BUILDER_TYPES.includes(type as BuilderType)) throw new NotRepresentable();
  const field = newBuilderField(name);
  field.type = type as BuilderType;
  field.nullable = nullable;
  field.required = required;
  const { type: _t, description, ...rest } = node;
  void _t;
  if (description !== undefined && typeof description !== "string") throw new NotRepresentable();
  field.description = (description as string | undefined) ?? "";
  if (type === "string" && Array.isArray(rest.enum) && rest.enum.every((v) => typeof v === "string")) {
    field.enumValues = rest.enum as string[];
    delete rest.enum;
  }
  if (type === "object") {
    const inner = objectFields(rest);
    field.children = inner.fields;
    field.rest = inner.rest;
    field.keepRequired = inner.keepRequired;
    return field;
  }
  if (type === "array") {
    const { items, ...arrayRest } = rest;
    if (!isRecord(items)) throw new NotRepresentable();
    const [itemType, itemNullable] = splitType(items.type);
    if (itemNullable || !BUILDER_TYPES.includes(itemType as BuilderType) || itemType === "array") throw new NotRepresentable();
    field.itemType = itemType as BuilderItemType;
    field.rest = arrayRest;
    const { type: _it, ...itemRest } = items;
    void _it;
    if (itemType === "object") {
      const inner = objectFields(itemRest);
      field.children = inner.fields;
      field.itemRest = inner.rest;
      field.keepRequired = inner.keepRequired;
    } else {
      field.itemRest = itemRest;
    }
    return field;
  }
  field.rest = rest;
  return field;
}

/**
 * The builder rows for an output schema, or null when the builder cannot show it (not an object
 * root, a property without a single `type`, an array of arrays, a nullable array item...). Those
 * schemas are edited in the raw tab.
 */
export function builderFromSchema(schema: unknown): BuilderRoot | null {
  if (!isRecord(schema) || schema.type !== "object") return null;
  try {
    return objectFields(schema);
  } catch (error) {
    if (error instanceof NotRepresentable) return null;
    throw error;
  }
}

/**
 * Property names as the builder writes them: a blank name stays blank and the second and later uses
 * of a name get `#2`, `#3`... Both break the subset's name rule, so the spec cannot be saved until
 * the row is fixed, and no row is ever silently dropped.
 */
function propertyKeys(fields: BuilderField[]): string[] {
  const seen = new Map<string, number>();
  return fields.map((field) => {
    const count = (seen.get(field.name) ?? 0) + 1;
    seen.set(field.name, count);
    return count === 1 ? field.name : `${field.name}#${count}`;
  });
}

function objectNode(fields: BuilderField[], rest: Record<string, unknown>, keepRequired: boolean): Record<string, unknown> {
  const keys = propertyKeys(fields);
  const properties: Record<string, unknown> = {};
  fields.forEach((field, i) => {
    properties[keys[i]] = nodeFromField(field);
  });
  const required = fields.flatMap((field, i) => (field.required ? [keys[i]] : []));
  const node: Record<string, unknown> = { type: "object", ...rest, properties };
  if (required.length > 0 || keepRequired) node.required = required;
  return node;
}

function nodeFromField(field: BuilderField): unknown {
  if (field.origin && nodeSignature(field) === field.origin.signature) return field.origin.node;
  const type = field.nullable ? [field.type, "null"] : field.type;
  const head: Record<string, unknown> = { type };
  if (field.description) head.description = field.description;
  if (field.type === "object") return { ...head, ...objectNode(field.children, field.rest, field.keepRequired), type };
  if (field.type === "array") {
    const items = field.itemType === "object" ? objectNode(field.children, field.itemRest, field.keepRequired) : { type: field.itemType, ...field.itemRest };
    return { ...head, ...field.rest, items };
  }
  const node: Record<string, unknown> = { ...head, ...field.rest };
  if (field.type === "string" && field.enumValues !== null) node.enum = field.enumValues;
  return node;
}

/** The output schema the builder rows stand for. */
export function schemaFromBuilder(root: BuilderRoot): Record<string, unknown> {
  return objectNode(root.fields, root.rest, root.keepRequired);
}

/** Problems with one builder row as typed (shown on the row; the schema check covers the rest). */
export function builderFieldProblems(field: BuilderField, siblings: BuilderField[]): string[] {
  const problems: string[] = [];
  if (!field.name) problems.push("Name is required");
  else if (!PROPERTY_NAME_PATTERN.test(field.name)) problems.push("Use letters, digits, _ and - only (at most 64)");
  else if (siblings.filter((other) => other.name === field.name).length > 1) problems.push(`Duplicate field name '${field.name}'`);
  if (field.type === "string" && field.enumValues !== null) {
    if (field.enumValues.length === 0) problems.push("List at least one allowed value");
    else if (new Set(field.enumValues).size !== field.enumValues.length) problems.push("Allowed values must be unique");
  }
  return problems;
}

/** A row changed to `type`: keywords that only fit the old type are dropped (a `title` stays). */
export function withBuilderType(field: BuilderField, type: BuilderType): BuilderField {
  if (type === field.type) return field;
  const keep = (rest: Record<string, unknown>) => ("title" in rest ? { title: rest.title } : {});
  return { ...field, type, rest: keep(field.rest), itemRest: {}, enumValues: null, children: type === "object" || type === "array" ? field.children : [] };
}

// ------------------------------------------------------------------ "Add citations field"

export const CITATIONS_FIELD = "citations";
export const CITATIONS_DESCRIPTION = "Source numbers [n] used";

/** The schema has a top-level `citations` property already. */
export function hasCitationsField(schema: unknown): boolean {
  return isRecord(schema) && isRecord(schema.properties) && CITATIONS_FIELD in schema.properties;
}

/**
 * The schema with `citations: array of integer` ("Source numbers [n] used") added at the top level,
 * once: a schema that has the property already (or is not an object) is returned unchanged.
 */
export function withCitationsField(schema: unknown): unknown {
  if (!isRecord(schema) || hasCitationsField(schema)) return schema;
  const properties = isRecord(schema.properties) ? schema.properties : {};
  return { ...schema, properties: { ...properties, [CITATIONS_FIELD]: { type: "array", description: CITATIONS_DESCRIPTION, items: { type: "integer" } } } };
}

// ------------------------------------------------------------------ values as saved

/** A new structured output: one required string field. */
export const STARTER_OUTPUT_SCHEMA = {
  type: "object",
  properties: { answer: { type: "string", description: "The answer" } },
  required: ["answer"],
};

/** `output` as saved: unset optional fields dropped. */
export function outputValue(output: OutputSpecValue | null): OutputSpecValue | undefined {
  if (!output) return undefined;
  const out: OutputSpecValue = { json_schema: output.json_schema };
  if (output.mode != null) out.mode = output.mode;
  if (output.name != null && output.name !== "") out.name = output.name;
  if (output.description != null && output.description !== "") out.description = output.description;
  if (output.max_retries != null) out.max_retries = output.max_retries;
  return out;
}

/** `limits` as saved: unset caps dropped; undefined (no key) when every cap is unset. */
export function limitsValue(limits: LimitsValue | null): LimitsValue | undefined {
  if (!limits) return undefined;
  const out: LimitsValue = {};
  for (const [key, value] of Object.entries(limits)) {
    if (value !== undefined && value !== null && value !== "") (out as Record<string, unknown>)[key] = value;
  }
  return Object.keys(out).length > 0 ? out : undefined;
}

/** `approvals` as saved: undefined (no key) without gated tools. */
export function approvalsValue(approvals: ApprovalPolicyValue | null): ApprovalPolicyValue | undefined {
  if (!approvals || approvals.tools.length === 0) return undefined;
  const out: ApprovalPolicyValue = { tools: approvals.tools };
  if (approvals.timeout_hours != null) out.timeout_hours = approvals.timeout_hours;
  return out;
}

/** `fallback_models` as saved: undefined (no key) while empty. */
export function fallbackModelsValue(models: ModelRefValue[]): ModelRefValue[] | undefined {
  return models.length > 0 ? models.map((m) => ({ provider: m.provider, name: m.name })) : undefined;
}

/** The approval ref of an MCP tool: `"<server>/<tool>"`. */
export function mcpApprovalRef(ref: McpToolRef): string {
  return `${ref.server}/${ref.tool}`;
}

/**
 * `approvals` after the agent's tools changed: entries naming a tool that is no longer selected are
 * dropped (a stale entry would fail the save), and the key goes once nothing is gated.
 */
export function prunedApprovals(approvals: ApprovalPolicyValue | null, tools: string[], mcpTools: McpToolRef[]): ApprovalPolicyValue | undefined {
  if (!approvals) return undefined;
  const known = new Set([...tools, ...mcpTools.map(mcpApprovalRef)]);
  return approvalsValue({ ...approvals, tools: approvals.tools.filter((ref) => known.has(ref)) });
}

// ------------------------------------------------------------------ suggestions

/** Why approval is suggested for a built-in tool (it has side effects), or null. */
export function builtinApprovalSuggestion(tool: { side_effect: boolean } | undefined): string | null {
  return tool?.side_effect ? "Has side effects" : null;
}

/**
 * Why approval is suggested for an MCP tool, only on evidence: its listed annotations say
 * `destructiveHint`, or they do not say `readOnlyHint`. No annotations (the tools were not loaded,
 * or the API does not pass them on) means no suggestion.
 */
export function mcpApprovalSuggestion(annotations?: { readOnlyHint?: boolean; destructiveHint?: boolean } | null): string | null {
  if (!annotations) return null;
  if (annotations.destructiveHint) return "Marked destructive";
  if (!annotations.readOnlyHint) return "Not marked read-only";
  return null;
}

// ------------------------------------------------------------------ problems (block saving)

export interface CapabilityProblem {
  /** The element id suffix (`output-<field>`, `limits-<field>`, `fallback-<i>`, `approvals-<field>`). */
  field: string;
  message: string;
}

function integerProblem(value: unknown, min: number, max: number, label: string): string | null {
  if (value === undefined || value === null || value === "") return null;
  if (typeof value !== "number" || !Number.isInteger(value) || value < min || value > max) return `${label} must be a whole number from ${min} to ${max}`;
  return null;
}

/** Problems with `spec.output` (none while it is off). */
export function outputProblems(spec: AgentSpec | null | undefined): CapabilityProblem[] {
  const output = outputOf(spec);
  if (!output) return [];
  const problems: CapabilityProblem[] = [];
  const schema = outputSchemaProblem(output.json_schema);
  if (schema) problems.push({ field: "schema", message: schema });
  if (output.name != null && output.name !== "" && !OUTPUT_NAME_PATTERN.test(output.name)) {
    problems.push({ field: "name", message: "The name may use letters, digits, _ and - only (at most 64)" });
  }
  if (typeof output.description === "string" && output.description.length > MAX_OUTPUT_DESCRIPTION_CHARS) {
    problems.push({ field: "description", message: `The description must be at most ${MAX_OUTPUT_DESCRIPTION_CHARS} characters` });
  }
  const retries = integerProblem(output.max_retries, 0, MAX_OUTPUT_RETRIES, "Retries");
  if (retries) problems.push({ field: "max_retries", message: retries });
  return problems;
}

// A plain decimal amount; at most 4 decimals once trailing zeros are dropped (as the server checks).
const COST_PATTERN = /^\d+(\.\d+)?$/;

/** The cost cap's problem as typed ("0.5" or a number), or null. */
export function costProblem(value: unknown): string | null {
  if (value === undefined || value === null || value === "") return null;
  const text = typeof value === "number" ? String(value) : typeof value === "string" ? value.trim() : "";
  const message = `The cost limit must be a dollar amount from ${MIN_COST_USD} to ${MAX_COST_USD} with at most 4 decimals`;
  if (!COST_PATTERN.test(text)) return message;
  if ((normaliseUsd(text).split(".")[1] ?? "").length > 4) return message;
  const amount = Number(text);
  return amount < MIN_COST_USD || amount > MAX_COST_USD ? message : null;
}

/**
 * A valid cost as the server stores it (eap_schemas.limits `normalise_usd`: `Decimal.normalize()`,
 * then no exponent): "0.50" -> "0.5", "1.0" -> "1", "007.10" -> "7.1", "1000" stays "1000". Text
 * that is not a plain decimal amount is returned unchanged (the save check reports
 * it). Storing the normal form keeps the draft equal to what the server sends back after a save.
 */
export function normaliseUsd(text: string): string {
  const trimmed = text.trim();
  if (!COST_PATTERN.test(trimmed)) return text;
  const [whole, fraction = ""] = trimmed.split(".");
  const intPart = whole.replace(/^0+(?=\d)/, "");
  const fracPart = fraction.replace(/0+$/, "");
  return fracPart ? `${intPart}.${fracPart}` : intPart;
}

/** Problems with `spec.limits`. */
export function limitsProblems(spec: AgentSpec | null | undefined): CapabilityProblem[] {
  const limits = limitsOf(spec);
  if (!limits) return [];
  const problems: CapabilityProblem[] = [];
  for (const { key, label, max } of LIMIT_FIELDS) {
    const problem = integerProblem(limits[key], 1, max, label);
    if (problem) problems.push({ field: key, message: problem });
  }
  const cost = costProblem(limits.max_cost_usd);
  if (cost) problems.push({ field: "max_cost_usd", message: cost });
  return problems;
}

/** Problems with `spec.fallback_models`: unchosen, the primary model, or a duplicate. */
export function fallbackProblems(spec: AgentSpec | null | undefined): CapabilityProblem[] {
  const models = fallbackModelsOf(spec);
  const primary = spec?.model ? `${spec.model.provider}:${spec.model.name}` : "";
  const problems: CapabilityProblem[] = [];
  if (models.length > MAX_FALLBACK_MODELS) problems.push({ field: "fallback-add", message: `At most ${MAX_FALLBACK_MODELS} fallback models` });
  const seen = new Set<string>();
  models.forEach((model, i) => {
    const id = `${model.provider}:${model.name}`;
    if (!model.provider || !model.name) problems.push({ field: `fallback-${i}`, message: "Choose a model" });
    else if (id === primary) problems.push({ field: `fallback-${i}`, message: "A fallback model cannot be the agent's own model" });
    else if (seen.has(id)) problems.push({ field: `fallback-${i}`, message: "This model is already a fallback" });
    seen.add(id);
  });
  return problems;
}

/** Problems with `spec.approvals`: a timeout out of range, an entry naming no tool of the agent. */
export function approvalProblems(spec: AgentSpec | null | undefined): CapabilityProblem[] {
  const approvals = approvalsOf(spec);
  if (!approvals) return [];
  const problems: CapabilityProblem[] = [];
  const known = new Set([...(spec?.tools ?? []), ...(spec?.mcp_tools ?? []).map(mcpApprovalRef)]);
  approvals.tools.forEach((ref, i) => {
    if (!known.has(ref)) problems.push({ field: `stale-${i}`, message: `'${ref}' is not a tool of this agent` });
  });
  if (approvals.tools.length > MAX_APPROVAL_TOOLS) problems.push({ field: "tools", message: `At most ${MAX_APPROVAL_TOOLS} tools can require approval` });
  const timeout = integerProblem(approvals.timeout_hours, 1, MAX_APPROVAL_TIMEOUT_HOURS, "The timeout");
  if (timeout) problems.push({ field: "timeout_hours", message: timeout });
  return problems;
}

// ------------------------------------------------------------------ status lines and error routing

function plural(count: number, one: string, many = `${one}s`): string {
  return `${count} ${count === 1 ? one : many}`;
}

const MODE_LABEL: Record<OutputMode, string> = { tool: "Tool mode", native: "Native mode", prompted: "Prompted mode" };

export function outputModeLabel(mode: OutputMode): string {
  return MODE_LABEL[mode];
}

/** The Output step's status line: "Off", "Tool mode · 3 fields", or "2 problems". */
export function outputStatus(spec: AgentSpec | null | undefined): { text: string; valid: boolean } {
  const problems = outputProblems(spec).length;
  if (problems > 0) return { text: plural(problems, "problem"), valid: false };
  const output = outputOf(spec);
  if (!output) return { text: "Off", valid: true };
  return { text: `${outputModeLabel(effectiveOutputMode(output))} · ${plural(outputFieldNames(output).length, "field")}`, valid: true };
}

/** Whether a save or publish message without a location is about `output` (routed to the Output step). */
export function isOutputError(message: string): boolean {
  return /^(Value error, )?output(\.[a-z_]+)*[:\s]/.test(message) || /^(Value error, )?output mode/i.test(message);
}

/** Whether a message is about `limits` or `fallback_models` (the Identity step's group). */
export function isIdentityCapabilityError(message: string): boolean {
  return /^(Value error, )?(limits(\.[a-z_]+)*|fallback_models(\[\d+\])?|max_cost_usd)[.:\s]/.test(message);
}

/** Whether a message is about `approvals` (the Tools step). */
export function isApprovalError(message: string): boolean {
  return /^(Value error, )?approvals(\.[a-z_]+)*(\[\d+\])?[.:\s]/.test(message) || /^(Value error, )?duplicate approval tool/.test(message);
}

// ------------------------------------------------------------------ publish warnings (AC.7)

/** Publish-time advice from the publish and restore responses; never blocks a publish. */
export interface PublishWarning {
  code: string;
  path: string;
  message: string;
}

/** The `warnings` of a publish or restore response (an empty list when absent or malformed). */
export function publishWarningsOf(response: unknown): PublishWarning[] {
  const list = isRecord(response) ? response.warnings : undefined;
  if (!Array.isArray(list)) return [];
  return list.filter((w): w is PublishWarning => isRecord(w) && typeof w.code === "string" && typeof w.message === "string" && typeof w.path === "string");
}
