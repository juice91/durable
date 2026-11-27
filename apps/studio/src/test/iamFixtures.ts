// Capability and explain fixtures for the three tenant roles, following the OpenFGA model
// (plugins/iam-openfga/model/durable.fga) for a person with no per-resource grant.
import type { Decision, Explanation } from "@/lib/access";
import type { IamCapabilities } from "@/lib/iam";

export type TestRole = "viewer" | "editor" | "admin";

const RANK: Record<TestRole, number> = { viewer: 1, editor: 2, admin: 3 };

/** The smallest role that holds `kind.action` (no grant). */
export function neededRole(kind: string, action: string): TestRole {
  if (kind === "tenant") return action === "read" ? "viewer" : action === "write" || action === "run" || action === "read_external_access" ? "editor" : "admin";
  if (action === "read") return "viewer";
  if (kind === "secret" || kind === "api_key") return "admin";
  if (kind === "mcp_server") return action === "run" ? "editor" : "admin";
  if (action === "approve" || action === "archive") return "admin";
  return "editor";
}

export function allows(role: TestRole, kind: string, action: string): boolean {
  return RANK[role] >= RANK[neededRole(kind, action)];
}

export function capabilitiesFor(role: TestRole): IamCapabilities {
  const actions = ["manage_members", "read_audit", "read_permissions"].filter((a) => allows(role, "tenant", a)).map((a) => `tenant.${a}`);
  return {
    mode: "production",
    identity: [],
    membership: { name: "zitadel", mutable: true },
    actions,
    policy: { name: "openfga", transitional: false, features: ["grants", "explain", "list_filter"] },
    me: { subject: `zitadel:${role}`, email: null, tenant_id: "t", kind: "user", roles: [role], provider: "zitadel" },
  };
}

export function decisionFor(role: TestRole, kind: string, action: string): Decision {
  const allowed = allows(role, kind, action);
  const needed = neededRole(kind, action);
  const roles = (["admin", "editor", "viewer"] as const).filter((r) => RANK[r] >= RANK[needed]);
  const relations = kind === "tenant" || action === "create" ? [] : action === "run" ? ["owner", "editor", "runner"] : action === "approve" ? ["approver"] : ["owner"];
  return { action, allowed, reason: allowed ? "allowed" : "no_permission", needed: { roles: [...roles], relations } };
}

export function explanationFor(role: TestRole, kind: string, id: string | null, actions: readonly string[]): Explanation {
  return { supported: true, kind, id, roles: [role], decisions: actions.map((a) => decisionFor(role, kind, a)) };
}

/** The answer of an IAM route (`capabilities` or `explain`) for `role`, or null for any other URL. */
export function iamAnswer(role: TestRole, url: string): Response | null {
  const parsed = new URL(url, "http://studio.test");
  const json = (body: unknown) => new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } });
  if (parsed.pathname.endsWith("/api/v1/iam/capabilities")) return json(capabilitiesFor(role));
  if (parsed.pathname.endsWith("/api/v1/iam/explain")) {
    const q = parsed.searchParams;
    return json(explanationFor(role, q.get("kind") ?? "", q.get("id"), q.getAll("action")));
  }
  return null;
}
