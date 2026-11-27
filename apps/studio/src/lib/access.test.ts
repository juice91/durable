import { afterEach, describe, expect, it, vi } from "vitest";
import { apiErrorFromResponse } from "./api";
import { addAccessGrant, askFor, explainAccess, getResourceAccess, removeAccessGrant, type Decision } from "./access";
import { onRefusal, type Refusal } from "./refusals";

const answer = (body: unknown, status = 200) => new Response(status === 204 ? null : JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
afterEach(() => vi.unstubAllGlobals());

const decision = (over: Partial<Decision> = {}): Decision => ({ action: "run", allowed: false, reason: "no_permission", needed: { roles: ["admin", "editor"], relations: ["owner", "editor", "runner"] }, ...over });

describe("access API", () => {
  it("reads, grants, revokes and explains with encoded ids", async () => {
    const calls: [string, RequestInit | undefined][] = [];
    vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
      calls.push([url, init]);
      return answer({}, init?.method === "DELETE" ? 204 : 200);
    }));
    await getResourceAccess("secret", "a b");
    await addAccessGrant("workflow", "w1", { relation: "runner", role: "viewer" });
    await removeAccessGrant("workflow", "w1", { relation: "runner", subject_type: "user", subject: "zitadel:1" });
    await explainAccess("agent", "a1", ["run", "edit"]);
    await explainAccess("secret", null, ["create"]);
    expect(calls[0][0]).toContain("/api/v1/iam/access/secret/a%20b");
    expect(calls[1][0]).toContain("/api/v1/iam/access/workflow/w1/grants");
    expect(JSON.parse(String(calls[1][1]?.body))).toEqual({ relation: "runner", role: "viewer" });
    expect(calls[2][0]).toContain("/grants?relation=runner&subject_type=user&subject=zitadel%3A1");
    expect(calls[2][1]?.method).toBe("DELETE");
    expect(calls[3][0]).toContain("/api/v1/iam/explain?kind=agent&id=a1&action=run&action=edit");
    expect(calls[4][0]).toMatch(/explain\?kind=secret&action=create$/);
  });

  it("says who can't do what, and what to ask an admin for, from the engine's answer", () => {
    expect(askFor(decision(), "agent", ["viewer"])).toBe("Your role (viewer) can't run this agent. Ask an admin for the editor role or a runner grant on it.");
    expect(askFor(decision({ action: "create", needed: { roles: ["admin"], relations: [] } }), "secret", ["editor"])).toBe("Your role (editor) can't create a secret. Ask an admin for the admin role.");
    expect(askFor(decision({ action: "approve", needed: { roles: ["admin"], relations: ["approver"] } }), "workflow", ["editor", "viewer"])).toBe("Your roles (editor, viewer) can't approve runs of this workflow. Ask an admin for the admin role or an approver grant on it.");
    expect(askFor(decision({ reason: "not_a_member" }), "knowledge_base")).toBe("You can't run this knowledge base: you are not a member of this tenant. Ask an admin to add you with the editor role.");
    expect(askFor(decision({ reason: "engine_unavailable", needed: null }), "agent")).toBe("Permissions can't be checked: the authorization engine is unavailable. Try again, or ask an admin to check it.");
    expect(askFor(decision({ action: "manage_settings", needed: { roles: ["admin"], relations: [] } }), "tenant")).toBe("You can't change access or tenant settings. Ask an admin for the admin role.");
    expect(askFor(decision({ action: "write", needed: { roles: ["admin", "editor"], relations: [] } }), "tenant", ["viewer"])).toBe("Your role (viewer) can't make changes in this tenant. Ask an admin for the editor role.");
  });

  it("reports a 403 that names what was refused, and an unavailable engine, to whoever explains it", async () => {
    const seen: Refusal[] = [];
    const stop = onRefusal((r) => seen.push(r));
    const refused = { action: "publish", kind: "agent", id: "a1" };
    const error = await apiErrorFromResponse(answer({ detail: "not allowed", refused }, 403));
    await apiErrorFromResponse(answer({ detail: "authorization is unavailable", code: "authz_unavailable" }, 503));
    await apiErrorFromResponse(answer({ detail: "origin not allowed" }, 403));
    stop();
    expect(error.message).toBe("You can't publish this agent. Ask an admin for access.");
    expect(seen).toEqual([{ type: "refused", ...refused }, { type: "unavailable" }]);
  });
});
