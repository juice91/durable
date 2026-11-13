// Request context of an agent (P0.16): its typed fields, server validation, and a saved value per
// agent in browser storage that the playground (P0.19) preselects.
//
// - GET  /api/v1/agents/{id}/request-context?version=<n|draft>  fields and JSON schema
// - POST /api/v1/agents/{id}/request-context/validate           {version?, request_context?}
//   200 {valid: true, request_context} or 422 {detail: [{loc: ["body", "request_context", field], msg, type}]}
import { ApiError, BASE_URL, apiErrorFromResponse, apiHeaders } from "@/lib/api";
import type { JsonSchema } from "@/lib/tools";

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

// --- Saved value per agent (browser storage) -------------------------------------------------

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

function storage(): Storage | null {
  try {
    return typeof window === "undefined" ? null : window.localStorage;
  } catch {
    return null;
  }
}

/** The saved context of this agent, or null (none, unreadable, or storage unavailable). */
export function loadSavedContext(agentId: string): SavedRequestContext | null {
  try {
    const raw = storage()?.getItem(savedContextKey(agentId));
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
    const store = storage();
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
    const store = storage();
    if (!store) return false;
    store.removeItem(savedContextKey(agentId));
    return true;
  } catch {
    return false;
  }
}

/** The playground query flag that makes the saved preset win over the tab's remembered context. */
export const PRESET_CONTEXT_PARAM = "context";
export const PRESET_CONTEXT_VALUE = "preset";

/**
 * The playground URL that opens with this agent selected (P0.19 reads `agent`, `version` and the
 * saved context). `version` (a number or "draft"; null: latest) is the version the preset was
 * checked against; `preferPreset` adds `context=preset` so the saved preset replaces the context
 * the playground remembered for this agent in the tab.
 */
export function playgroundHref(agentId: string, version: ContextVersionRef = null, preferPreset = false): string {
  const params = new URLSearchParams({ agent: agentId });
  if (version !== null) params.set("version", String(version));
  if (preferPreset) params.set(PRESET_CONTEXT_PARAM, PRESET_CONTEXT_VALUE);
  return `/playground?${params.toString()}`;
}
