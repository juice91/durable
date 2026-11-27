// Agent-bound grants: the Access section of an agent and of a workflow, and the publish card's grant line
// (docs/iam.md, "Agent and workflow grants"). `GET /{agents|workflows}/{id}/access`, `POST .../grants`, `DELETE .../grants`,
// `GET .../publish-grants`. Names and flags only; the server is the enforcer (admin only for a write).
import { apiErrorFromResponse, apiHeaders, BASE_URL } from "./api";

export type GrantSubjectKind = "agent" | "workflow";
export type GrantResourceKind = "mcp_server" | "secret";

export interface UseGrant {
  kind: GrantResourceKind;
  id: string;
  name: string;
  /** The names of the credentials an MCP server carries (headers, env); never a value. Empty for a secret. */
  credentials: string[];
  /** The newest published version names it; false: granted but unused by the latest version; null: no published version. */
  used_by_latest: boolean | null;
  granted_by?: string;
  granted_at?: string;
}

export interface AccessOptions {
  mcp_servers: { id: string; name: string }[];
  secrets: string[];
}

export interface AccessStatus {
  subject: { kind: GrantSubjectKind; id: string };
  /** The policy engine keeps grants; false (the transitional engine): the section is not shown. */
  supported: boolean;
  /** The worker enforces them (EAP_AGENT_GRANTS_ENFORCE). */
  enforced: boolean;
  /** The caller is an admin: Add and Remove are offered. */
  can_manage: boolean;
  grants: UseGrant[];
  /** What an admin may add (only for an admin). */
  options: AccessOptions | null;
}

/** What publishing would newly grant (the publish card's line). */
export interface PublishGrants {
  newly_uses: { mcp_servers: string[]; secrets: string[] };
  can_approve: boolean;
  dynamic?: boolean;
  /** "This version newly uses MCP server `x`, secret `y`. Approving grants them."; null when nothing is new. */
  message: string | null;
}

const base = (kind: GrantSubjectKind, id: string) => `${BASE_URL}/api/v1/${kind === "agent" ? "agents" : "workflows"}/${encodeURIComponent(id)}`;

export async function getAccess(kind: GrantSubjectKind, id: string, signal?: AbortSignal): Promise<AccessStatus> {
  const response = await fetch(`${base(kind, id)}/access`, { headers: apiHeaders(), signal, cache: "no-store" });
  if (!response.ok) throw await apiErrorFromResponse(response);
  return response.json();
}

export async function addGrant(kind: GrantSubjectKind, id: string, resource: { kind: GrantResourceKind; name: string }): Promise<void> {
  const response = await fetch(`${base(kind, id)}/grants`, {
    method: "POST",
    headers: apiHeaders({ "Content-Type": "application/json" }),
    body: JSON.stringify(resource),
  });
  if (!response.ok) throw await apiErrorFromResponse(response);
}

export async function removeGrant(kind: GrantSubjectKind, id: string, resource: { kind: GrantResourceKind; name: string }): Promise<void> {
  const query = new URLSearchParams({ kind: resource.kind, name: resource.name });
  const response = await fetch(`${base(kind, id)}/grants?${query.toString()}`, { method: "DELETE", headers: apiHeaders() });
  if (!response.ok) throw await apiErrorFromResponse(response);
}

export async function getPublishGrants(kind: GrantSubjectKind, id: string, signal?: AbortSignal): Promise<PublishGrants> {
  const response = await fetch(`${base(kind, id)}/publish-grants`, { headers: apiHeaders(), signal, cache: "no-store" });
  if (!response.ok) throw await apiErrorFromResponse(response);
  return response.json();
}

/** Resources an admin could still add: the registered ones minus those already granted. */
export function addable(status: AccessStatus): { kind: GrantResourceKind; name: string; label: string }[] {
  if (!status.options) return [];
  const has = (kind: GrantResourceKind, name: string) => status.grants.some((g) => g.kind === kind && g.name === name);
  return [
    ...status.options.mcp_servers.filter((s) => !has("mcp_server", s.name)).map((s) => ({ kind: "mcp_server" as const, name: s.name, label: `MCP server · ${s.name}` })),
    ...status.options.secrets.filter((n) => !has("secret", n)).map((n) => ({ kind: "secret" as const, name: n, label: `Secret · ${n}` })),
  ];
}

/** The line under a grant: who granted it and when, the credentials a server carries, and "unused by the latest version". */
export function grantNotes(grant: UseGrant): string[] {
  const notes: string[] = [];
  if (grant.kind === "mcp_server" && grant.credentials.length > 0) notes.push(`This server uses credentials: ${grant.credentials.join(", ")}`);
  if (grant.granted_by) notes.push(`Granted by ${grant.granted_by}${grant.granted_at ? ` on ${new Date(grant.granted_at).toLocaleDateString()}` : ""}`);
  if (grant.used_by_latest === false) notes.push("Granted, unused by the latest version");
  return notes;
}

/** The readable refusal of a blocked use: the event the worker writes (`permission_denied`), as the run view shows it. */
export function permissionDeniedMessage(data: unknown): string | null {
  if (!data || typeof data !== "object") return null;
  const message = (data as { message?: unknown }).message;
  return typeof message === "string" && message ? message : null;
}
