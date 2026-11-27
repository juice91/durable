// The Access tab and "why can't I?" (docs/iam.md, "The Access tab and explain"): `GET|POST|DELETE /api/v1/iam/access/...`
// and `GET /api/v1/iam/explain`. Names, relations and roles only; the server is the enforcer (admin only for a change).
import { apiErrorFromResponse, apiHeaders, BASE_URL } from "./api";
import { peekCapabilities } from "./iam";
import { refusedOf } from "./refusals";

export const ACCESS_KINDS = ["agent", "workflow", "knowledge_base", "secret", "mcp_server"] as const;
export type AccessKind = (typeof ACCESS_KINDS)[number];

export interface AccessGrant {
  relation: string;
  subject_type: "user" | "role";
  subject: string;
}

export interface AccessView {
  resource: { kind: AccessKind; id: string };
  /** The policy engine keeps grants (false under `openaccess`). */
  supported: boolean;
  /** The caller is an admin: Add and Remove are offered. */
  can_manage: boolean;
  /** The model's relations for this kind, and the tenant roles that may hold each (none: people only). */
  relations: { relation: string; roles: string[] }[];
  grants: AccessGrant[];
  /** The agents and workflows granted this secret or MCP server (those the caller may read). */
  used_by: { kind: string; id: string }[];
}

export interface Decision {
  action: string;
  allowed: boolean;
  reason: "allowed" | "not_a_member" | "no_permission" | "engine_unavailable";
  needed: { roles: string[]; relations: string[] } | null;
}

export interface Explanation {
  supported: boolean;
  kind: string;
  id: string | null;
  roles: string[];
  decisions: Decision[];
}

const accessUrl = (kind: AccessKind, id: string) => `${BASE_URL}/api/v1/iam/access/${kind}/${encodeURIComponent(id)}`;

export async function getResourceAccess(kind: AccessKind, id: string, signal?: AbortSignal): Promise<AccessView> {
  const response = await fetch(accessUrl(kind, id), { headers: apiHeaders(), signal, cache: "no-store" });
  if (!response.ok) throw await apiErrorFromResponse(response);
  return response.json();
}

export async function addAccessGrant(kind: AccessKind, id: string, grant: { relation: string; member_id?: string; role?: string }): Promise<void> {
  const response = await fetch(`${accessUrl(kind, id)}/grants`, {
    method: "POST",
    headers: apiHeaders({ "Content-Type": "application/json" }),
    body: JSON.stringify(grant),
  });
  if (!response.ok) throw await apiErrorFromResponse(response);
}

export async function removeAccessGrant(kind: AccessKind, id: string, grant: AccessGrant): Promise<void> {
  const query = new URLSearchParams({ relation: grant.relation, subject_type: grant.subject_type, subject: grant.subject });
  const response = await fetch(`${accessUrl(kind, id)}/grants?${query.toString()}`, { method: "DELETE", headers: apiHeaders() });
  if (!response.ok) throw await apiErrorFromResponse(response);
}

/** The caller's own decisions on `actions`; `id` null asks about the collection (create) or the tenant. */
export async function explainAccess(kind: string, id: string | null, actions: readonly string[], signal?: AbortSignal): Promise<Explanation> {
  const query = new URLSearchParams({ kind });
  if (id !== null) query.set("id", id);
  actions.forEach((action) => query.append("action", action));
  const response = await fetch(`${BASE_URL}/api/v1/iam/explain?${query.toString()}`, { headers: apiHeaders(), signal, cache: "no-store" });
  if (!response.ok) throw await apiErrorFromResponse(response);
  return response.json();
}

const KIND_WORDS: Record<string, string> = {
  agent: "agent", workflow: "workflow", knowledge_base: "knowledge base", secret: "secret", mcp_server: "MCP server",
  schedule: "schedule", api_key: "API key", tenant: "tenant",
};

export function kindWord(kind: string): string {
  return KIND_WORDS[kind] ?? kind;
}

/** Who is refused, for the head of a sentence: "Your role (viewer)", or "You" when no role is known. */
function who(roles: readonly string[]): string {
  if (roles.length === 0) return "You";
  return `${roles.length === 1 ? "Your role" : "Your roles"} (${roles.join(", ")})`;
}

const article = (word: string) => (/^(a|e|i|o|u|API|MCP)/.test(word) ? "an" : "a");

const TENANT_ACTIONS: Record<string, string> = {
  read: "see this tenant", write: "make changes in this tenant", run: "run tools in this tenant", read_audit: "read the audit log",
  manage_members: "manage members", manage_settings: "change access or tenant settings", manage_connections: "manage source connections",
};

function canNot(action: string, kind: string, roles: readonly string[]): string {
  const head = `${who(roles)} can't`;
  if (kind === "tenant") return `${head} ${TENANT_ACTIONS[action] ?? `${action.replace(/_/g, " ")} in this tenant`}`;
  const word = kindWord(kind);
  if (action === "create") return `${head} create ${article(word)} ${word}`;
  const verbs: Record<string, string> = {
    read: "see", approve: "approve runs of", manage_access: "change who has access to", grant_use: "grant agents the use of",
    archive: kind === "agent" || kind === "workflow" ? "archive" : "delete",
  };
  return `${head} ${verbs[action] ?? action.replace(/_/g, " ")} this ${word}`;
}

// The smallest role and the narrowest grant that would allow it (the engine lists roles from the largest down).
const NARROWEST_FIRST = ["viewer", "runner", "approver", "editor", "owner"];

/** The sentence for a refusal: who, the action and the resource, and what to ask an admin for (the engine's answer). */
export function askFor(decision: Decision, kind: string, roles: readonly string[] = []): string {
  if (decision.reason === "engine_unavailable") {
    return "Permissions can't be checked: the authorization engine is unavailable. Try again, or ask an admin to check it.";
  }
  const needed = decision.needed?.roles ?? [];
  const role = needed.length > 0 ? `the ${needed[needed.length - 1]} role` : "";
  if (decision.reason === "not_a_member") {
    return `${canNot(decision.action, kind, [])}: you are not a member of this tenant. Ask an admin to add you${role ? ` with ${role}` : ""}.`;
  }
  const relations = decision.action === "create" || kind === "tenant" ? [] : (decision.needed?.relations ?? []);
  const relation = NARROWEST_FIRST.find((r) => relations.includes(r));
  const grant = relation ? `${article(relation)} ${relation} grant on it` : "";
  const asks = [role, grant].filter(Boolean).join(" or ");
  return `${canNot(decision.action, kind, roles)}. Ask an admin for ${asks || "access"}.`;
}

/** How long a refused call waits for the engine's reason before it says what it knows. */
const EXPLAIN_TIMEOUT_MS = 4000;

/**
 * The message of a 403 that names what the policy engine refused (`refused: {action, kind, id}`): the engine's own reason
 * when it can explain, else who, the action and the kind. Null for every other answer, which keeps the server's words.
 */
export async function refusedMessage(status: number, body: unknown): Promise<string | null> {
  const refused = refusedOf(status, body);
  if (!refused) return null;
  const caps = peekCapabilities();
  let roles = caps?.me.roles ?? [];
  if (caps?.policy.features.includes("explain")) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), EXPLAIN_TIMEOUT_MS);
    try {
      const answer = await explainAccess(refused.kind, refused.id, [refused.action], controller.signal);
      roles = answer.roles ?? roles;
      const denied = answer.decisions.find((d) => !d.allowed);
      if (denied) return askFor(denied, refused.kind, roles);
    } catch {
      // No reason to be had: say what is known.
    } finally {
      clearTimeout(timer);
    }
  }
  return askFor({ action: refused.action, allowed: false, reason: "no_permission", needed: null }, refused.kind, roles);
}
