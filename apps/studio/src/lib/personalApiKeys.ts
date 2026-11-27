// Personal API keys: a signed-in person's own keys (/api/v1/iam/me/api-keys; docs/iam.md, "Personal API keys").
//
// The plaintext `key` is in the POST answer only, once. Studio hands it to the create dialog's state and nowhere
// else: never storage, the URL, a toast or a log. The list answer has no secret.
import { ApiError, apiErrorFromResponse, apiHeaders, BASE_URL } from "./api";
import type { IamMe } from "./iam";

export type PersonalKeyStatus = "active" | "revoked" | "expired";
export type PersonalScope = "agui:run" | "mcp" | "manage" | "otlp";

export interface PersonalApiKey {
  id: string;
  name: string;
  prefix: string;
  status: PersonalKeyStatus;
  scopes: string[];
  created_at: string;
  expires_at: string | null;
  revoked_at: string | null;
  last_used_at: string | null;
}

export interface PersonalApiKeyCreated extends PersonalApiKey {
  key: string;
}

export const PERSONAL_SCOPES: { scope: PersonalScope; label: string; hint: string }[] = [
  { scope: "mcp", label: "mcp", hint: "Read agents, workflows, runs and knowledge bases over MCP." },
  { scope: "agui:run", label: "agui:run", hint: "Run agents and workflows over AG-UI." },
  { scope: "manage", label: "manage", hint: "Change things over MCP: publish, start runs, decide approvals." },
  { scope: "otlp", label: "otlp", hint: "Send OpenTelemetry traces." },
];

/** `eap_iam.ROLE_SCOPES`: what each role may put on a key. The server is the enforcer; this only greys out the rest. */
const ROLE_SCOPES: Record<string, PersonalScope[]> = {
  viewer: ["mcp"],
  editor: ["agui:run", "mcp", "manage"],
  admin: ["agui:run", "mcp", "manage", "otlp"],
};

export const MAX_EXPIRY_DAYS = 365;
export const EXPIRY_PRESET_DAYS = [30, 90, 365] as const;
const DAY_MS = 86_400_000;

/** Personal keys belong to a person: the zitadel, cf-access and built-in admin sessions. The loopback dev bearer and API keys are not. */
export function isPersonSession(me: IamMe): boolean {
  return me.provider === "zitadel" || me.provider === "cf-access" || me.provider === "localadmin";
}

/** The scopes the person's roles allow. No recorded role (no membership source): the owner counts as admin, as the server does. */
export function allowedScopes(roles: string[]): Set<PersonalScope> {
  const known = roles.filter((role) => role in ROLE_SCOPES);
  if (known.length === 0) return new Set(ROLE_SCOPES.admin);
  return new Set(known.flatMap((role) => ROLE_SCOPES[role]));
}

/** Why a scope is greyed out, or null when it may be chosen. */
export function scopeDisabledReason(scope: PersonalScope, roles: string[]): string | null {
  if (allowedScopes(roles).has(scope)) return null;
  const need = scope === "otlp" ? "an admin" : "an editor or an admin";
  return `Needs ${need}; your role${roles.length === 1 ? " is " : "s are "}${roles.join(", ") || "none"}.`;
}

/** The day count for a custom date (whole days, rounded up), so it can be checked against the 365-day cap. */
export function daysUntil(date: Date, now: Date = new Date()): number {
  return Math.ceil((date.getTime() - now.getTime()) / DAY_MS);
}

export function expiryFromDays(days: number, now: Date = new Date()): string {
  return new Date(now.getTime() + days * DAY_MS).toISOString();
}

/** What the person reads for an error from these routes; 403 `not_a_person` means "sign in", not "ask an admin". */
export function personalKeyErrorMessage(reason: unknown): string {
  if (reason instanceof ApiError) {
    if (reason.status === 401 || (reason.status === 403 && /person/i.test(reason.message))) {
      return "Personal API keys belong to a signed-in person. Sign in with your own account (not the dev bearer or an API key) to manage them.";
    }
    if (reason.status === 403) return reason.message || "Your roles do not allow that.";
    if (reason.status === 409) return reason.message || "You have too many active keys; revoke one first.";
    return reason.message;
  }
  return reason instanceof Error ? reason.message : "Something went wrong.";
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const response = await fetch(`${BASE_URL}/api/v1/iam/me/api-keys${path}`, {
    ...init,
    cache: "no-store", // the create answer holds the secret
    headers: apiHeaders({ "Content-Type": "application/json" }),
  });
  if (!response.ok) throw await apiErrorFromResponse(response);
  if (response.status === 204) return undefined as T;
  return response.json();
}

export const personalApiKeys = {
  list: async (): Promise<PersonalApiKey[]> => (await request<{ items?: PersonalApiKey[] }>("")).items ?? [],
  create: (body: { name: string; scopes: PersonalScope[]; expires_at: string }) =>
    request<PersonalApiKeyCreated>("", { method: "POST", body: JSON.stringify(body) }),
  revoke: (id: string) => request<void>(`/${encodeURIComponent(id)}`, { method: "DELETE" }),
};
