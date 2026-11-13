// Request context of an agent (P0.16): its typed fields, server validation, and named presets per
// agent in browser storage that the chat's request context popover (SL.10) saves and applies.
//
// - GET  /api/v1/agents/{id}/request-context?version=<n|draft>  fields and JSON schema
// - POST /api/v1/agents/{id}/request-context/validate           {version?, request_context?}
//   200 {valid: true, request_context} or 422 {detail: [{loc: ["body", "request_context", field], msg, type}]}
import { ApiError, BASE_URL, apiErrorFromResponse, apiHeaders } from "@/lib/api";
import type { JsonSchema } from "@/lib/tools";
import { safeLocalStorage } from "@/lib/safeStorage";

export type ContextFieldType = "string" | "integer" | "number" | "boolean" | "enum";

export interface ContextField {
  name: string;
  type: ContextFieldType;
  required: boolean;
  description: string;
  max_length: number | null;
  /** Enum fields only: the allowed values. */
  values: string[];
}

/** Which spec a context is checked against: a published version, the draft, or (null) the latest version. */
export type ContextVersionRef = number | "draft" | null;

export interface RequestContextInfo {
  agent_id: string;
  /** The resolved published version; null for the draft. */
  version: number | null;
  draft: boolean;
  fields: ContextField[];
  schema: JsonSchema;
}

export type RequestContextValue = Record<string, unknown>;

/**
 * A refused context. `fieldErrors` maps a field name to its message (several joined by "; ");
 * `banner` holds whole-object problems (size, not an object) and any non-field error.
 */
export class RequestContextValidationError extends Error {
  constructor(
    public status: number,
    public banner: string | null,
    public fieldErrors: Record<string, string>,
  ) {
    super(banner ?? (Object.values(fieldErrors).join(", ") || `HTTP ${status}`));
    this.name = "RequestContextValidationError";
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function agentPath(agentId: string): string {
  return `${BASE_URL}/api/v1/agents/${encodeURIComponent(agentId)}/request-context`;
}

export async function getRequestContext(agentId: string, version: ContextVersionRef = null): Promise<RequestContextInfo> {
  const query = version === null ? "" : `?${new URLSearchParams({ version: String(version) }).toString()}`;
  const response = await fetch(`${agentPath(agentId)}${query}`, { headers: apiHeaders() });
  if (!response.ok) throw await apiErrorFromResponse(response);
  const data: unknown = await response.json();
  if (!isRecord(data) || !Array.isArray(data.fields) || !isRecord(data.schema)) {
    throw new Error("Invalid request context response");
  }
  return data as unknown as RequestContextInfo;
}

/** Maps a 422 `detail` (list or string) to field errors and a banner. */
export function mapContextErrors(status: number, detail: unknown): RequestContextValidationError {
  if (!Array.isArray(detail)) {
    return new RequestContextValidationError(status, typeof detail === "string" ? detail : `HTTP ${status}`, {});
  }
  const fieldErrors: Record<string, string> = {};
  const general: string[] = [];
  for (const item of detail) {
    const loc = isRecord(item) && Array.isArray(item.loc) ? item.loc.map(String) : [];
    const msg = isRecord(item) && typeof item.msg === "string" ? item.msg : "is invalid";
    const field = loc[0] === "body" && loc[1] === "request_context" ? loc[2] : undefined;
    if (field === undefined) {
      general.push(loc[0] === "body" && loc[1] === "request_context" ? msg : loc.length > 0 ? `${loc.join(".")}: ${msg}` : msg);
      continue;
    }
    fieldErrors[field] = fieldErrors[field] ? `${fieldErrors[field]}; ${msg}` : msg;
  }
  const banner = general.length > 0 ? general.join("; ") : null;
  return new RequestContextValidationError(status, banner, fieldErrors);
}

/**
 * Checks `value` against the version's fields. Resolves to the accepted context (declared fields
 * only); rejects with RequestContextValidationError on a 422, ApiError otherwise.
 */
export async function validateRequestContext(
  agentId: string,
  value: RequestContextValue,
  version: ContextVersionRef = null,
  signal?: AbortSignal,
): Promise<RequestContextValue> {
  const body: Record<string, unknown> = { request_context: value };
  if (version !== null) body.version = version;
  const response = await fetch(`${agentPath(agentId)}/validate`, {
    method: "POST",
    headers: apiHeaders({ "Content-Type": "application/json" }),
    body: JSON.stringify(body),
    signal,
  });
  if (response.status === 422) {
    let detail: unknown = null;
    try {
      detail = (await response.json())?.detail;
    } catch {
      // Not JSON: fall back to the status.
    }
    throw mapContextErrors(422, detail);
  }
  if (!response.ok) throw await apiErrorFromResponse(response);
  const data: unknown = await response.json();
  if (!isRecord(data) || !isRecord(data.request_context)) throw new ApiError(response.status, "Invalid validation response");
  return data.request_context;
}

/**
 * The value to send: undefined and null entries are left out (null means "not supplied" to the
 * API). In form mode (the default) "" is left out too, because an empty input or an unselected
 * enum means "not supplied"; in JSON mode (`keepEmptyStrings`) an explicit "" is deliberate and kept.
 */
export function normalizeContext(
  value: RequestContextValue,
  options: { keepEmptyStrings?: boolean } = {},
): RequestContextValue {
  const result: RequestContextValue = {};
  for (const [key, item] of Object.entries(value)) {
    if (item === undefined || item === null) continue;
    if (item === "" && !options.keepEmptyStrings) continue;
    result[key] = item;
  }
  return result;
}

/** Parses JSON-mode text: a JSON object, or a message. */
export function parseContextJson(raw: string): { value: RequestContextValue } | { error: string } {
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch {
    return { error: "Enter valid JSON" };
  }
  if (!isRecord(parsed)) return { error: "The request context must be a JSON object" };
  return { value: parsed };
}

export function formatContextJson(value: RequestContextValue): string {
  return JSON.stringify(value, null, 2);
}

// --- The old single saved value per agent (P0.16; read only to migrate it into presets) -------

export const SAVED_CONTEXT_PREFIX = "eap.requestContext.v1:";

export interface SavedRequestContext {
  /** The context as the server accepted it. */
  value: RequestContextValue;
  /** The version it was validated against (null: latest at the time). */
  version: ContextVersionRef;
  saved_at: string;
}

/**
 * A one-way fingerprint of the API credential Studio is configured with. Studio does not know the
 * tenant or user (the API derives them from the credential and exposes no "who am I"), so saved
 * values are scoped by credential instead: another credential in the same browser never sees them.
 * The credential itself is never stored.
 */
export function credentialScope(): string {
  const token = process.env.NEXT_PUBLIC_EAP_DEV_TOKEN || "";
  if (!token) return "anon";
  // FNV-1a, two 32-bit lanes; not a security boundary, only a namespace.
  let a = 0x811c9dc5;
  let b = 0x01000193 ^ token.length;
  for (let i = 0; i < token.length; i += 1) {
    const code = token.charCodeAt(i);
    a = Math.imul(a ^ code, 0x01000193) >>> 0;
    b = Math.imul(b ^ code, 0x5bd1e995) >>> 0;
  }
  return `c${a.toString(16).padStart(8, "0")}${b.toString(16).padStart(8, "0")}`;
}

/** `eap.requestContext.v1:<credential scope>:<agentId>`. */
export function savedContextKey(agentId: string): string {
  return `${SAVED_CONTEXT_PREFIX}${credentialScope()}:${agentId}`;
}

/** The saved context of this agent, or null (none, unreadable, or storage unavailable). */
export function loadSavedContext(agentId: string): SavedRequestContext | null {
  try {
    const raw = safeLocalStorage()?.getItem(savedContextKey(agentId));
    if (!raw) return null;
    const parsed: unknown = JSON.parse(raw);
    if (!isRecord(parsed) || !isRecord(parsed.value)) return null;
    const version = parsed.version;
    const validVersion = version === null || version === "draft" || (typeof version === "number" && Number.isInteger(version) && version > 0);
    return {
      value: parsed.value,
      version: validVersion ? version as ContextVersionRef : null,
      saved_at: typeof parsed.saved_at === "string" ? parsed.saved_at : "",
    };
  } catch {
    return null;
  }
}

/** Saves the context for this agent; null when browser storage is unavailable or full. */
export function saveContext(agentId: string, value: RequestContextValue, version: ContextVersionRef): SavedRequestContext | null {
  const saved: SavedRequestContext = { value, version, saved_at: new Date().toISOString() };
  try {
    const store = safeLocalStorage();
    if (!store) return null;
    store.setItem(savedContextKey(agentId), JSON.stringify(saved));
    return saved;
  } catch {
    return null;
  }
}

/** Removes this agent's saved context; false when storage is unavailable. */
export function clearSavedContext(agentId: string): boolean {
  try {
    const store = safeLocalStorage();
    if (!store) return false;
    store.removeItem(savedContextKey(agentId));
    return true;
  } catch {
    return false;
  }
}

/** The chat query flag (`context=…`): `preset` applies the newest preset over the tab's remembered
 * context; `open` opens the request context popover. Both are read once and removed from the URL. */
export const PRESET_CONTEXT_PARAM = "context";
export const PRESET_CONTEXT_VALUE = "preset";
export const OPEN_CONTEXT_VALUE = "open";

// --- Named presets per agent (SL.10) --------------------------------------------------------
//
// `localStorage["studio.requestContextPresets.v1.<credential scope>:<agentId>"]` holds
// `{presets: ContextPreset[], migrated: true}`. The first read of an agent imports its old single
// saved value (`eap.requestContext.v1:…`, the P0.16 Request Context page) as a preset named "Saved"
// and marks the record migrated, so a later read never imports it again, even after "Saved" is
// deleted. The old key is left in place for one milestone.

export const PRESETS_PREFIX = "studio.requestContextPresets.v1.";
/** The name the old saved value gets when it is imported. */
export const MIGRATED_PRESET_NAME = "Saved";
export const PRESET_NAME_MAX_LENGTH = 60;

export interface ContextPreset {
  name: string;
  /** The context as the server accepted it. */
  value: RequestContextValue;
  /** The version it was validated against (null: the latest at the time). */
  version: ContextVersionRef;
  saved_at: string;
}

interface PresetRecord {
  presets: ContextPreset[];
  migrated: boolean;
}

/** `studio.requestContextPresets.v1.<credential scope>:<agentId>`. */
export function presetsKey(agentId: string): string {
  return `${PRESETS_PREFIX}${credentialScope()}:${agentId}`;
}

function validVersion(version: unknown): ContextVersionRef {
  return version === "draft" || (typeof version === "number" && Number.isInteger(version) && version > 0) ? version : null;
}

function parsePreset(raw: unknown): ContextPreset | null {
  if (!isRecord(raw) || typeof raw.name !== "string" || !raw.name.trim() || !isRecord(raw.value)) return null;
  return {
    name: raw.name,
    value: raw.value,
    version: validVersion(raw.version),
    saved_at: typeof raw.saved_at === "string" ? raw.saved_at : "",
  };
}

/** The stored record; null when there is none, it is unreadable, or storage is unavailable. */
function readPresetRecord(agentId: string): PresetRecord | null {
  try {
    const raw = safeLocalStorage()?.getItem(presetsKey(agentId));
    if (!raw) return null;
    const parsed: unknown = JSON.parse(raw);
    if (!isRecord(parsed) || !Array.isArray(parsed.presets)) return null;
    const presets: ContextPreset[] = [];
    for (const item of parsed.presets) {
      const preset = parsePreset(item);
      if (preset && !presets.some((other) => other.name === preset.name)) presets.push(preset);
    }
    return { presets, migrated: parsed.migrated === true };
  } catch {
    return null;
  }
}

function writePresetRecord(agentId: string, presets: ContextPreset[]): boolean {
  try {
    const store = safeLocalStorage();
    if (!store) return false;
    const record: PresetRecord = { presets, migrated: true };
    store.setItem(presetsKey(agentId), JSON.stringify(record));
    return true;
  } catch {
    return false;
  }
}

/**
 * The agent's presets with the old saved value imported (once). Writes the record only when there
 * is something to import, so reading an agent with no presets stores nothing.
 */
function migratedPresets(agentId: string): ContextPreset[] {
  const record = readPresetRecord(agentId);
  if (record?.migrated) return record.presets;
  const presets = record?.presets ?? [];
  const old = loadSavedContext(agentId);
  if (old === null) return presets;
  const merged = presets.some((preset) => preset.name === MIGRATED_PRESET_NAME)
    ? presets
    : [...presets, { name: MIGRATED_PRESET_NAME, value: old.value, version: old.version, saved_at: old.saved_at }];
  // A failed write leaves the record unmarked: the import is tried again on the next read.
  writePresetRecord(agentId, merged);
  return merged;
}

/** Sorts presets by name, case-insensitively. */
function byName(a: ContextPreset, b: ContextPreset): number {
  return a.name.localeCompare(b.name, undefined, { sensitivity: "base" }) || a.name.localeCompare(b.name);
}

/** This agent's presets, by name (empty when none or storage is unavailable). Migrates the old key. */
export function listPresets(agentId: string): ContextPreset[] {
  return [...migratedPresets(agentId)].sort(byName);
}

/** The most recently saved preset, or null. */
export function latestPreset(agentId: string): ContextPreset | null {
  const presets = migratedPresets(agentId);
  let latest: ContextPreset | null = null;
  for (const preset of presets) if (latest === null || preset.saved_at > latest.saved_at) latest = preset;
  return latest;
}

/** A preset name problem, or null: 1 to 60 characters after trimming. */
export function presetNameError(name: string): string | null {
  const trimmed = name.trim();
  if (!trimmed) return "Enter a name";
  if (trimmed.length > PRESET_NAME_MAX_LENGTH) return `A name is at most ${PRESET_NAME_MAX_LENGTH} characters`;
  return null;
}

/**
 * Saves `value` as the preset `name` (trimmed), replacing a preset of the same name. Null when the
 * name is invalid or browser storage is unavailable or full.
 */
export function savePreset(
  agentId: string,
  name: string,
  value: RequestContextValue,
  version: ContextVersionRef,
): ContextPreset | null {
  if (presetNameError(name) !== null) return null;
  const preset: ContextPreset = { name: name.trim(), value, version, saved_at: new Date().toISOString() };
  const presets = migratedPresets(agentId);
  const index = presets.findIndex((item) => item.name === preset.name);
  const next = index === -1 ? [...presets, preset] : presets.map((item, i) => (i === index ? preset : item));
  return writePresetRecord(agentId, next) ? preset : null;
}

/** Deletes the preset `name`; false when browser storage is unavailable. Deleting a missing name succeeds. */
export function deletePreset(agentId: string, name: string): boolean {
  const presets = migratedPresets(agentId);
  return writePresetRecord(agentId, presets.filter((item) => item.name !== name));
}
