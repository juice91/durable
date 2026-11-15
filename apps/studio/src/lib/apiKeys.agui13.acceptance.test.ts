/** AGUI.13 acceptance: key form body, fail-closed allowlists, endpoints and secret-free snippets. */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  API_KEY_ENV_VAR,
  aguiAttachUrl,
  aguiEndpoint,
  aguiThreadUrl,
  apiKeys,
  buildApiKeyCreate,
  copilotKitSnippet,
  curlSnippet,
  describeAccess,
  withoutSecret,
  type ApiKeyCreated,
  type ApiKeyFormInput,
} from "./apiKeys";

const NOW = new Date("2026-11-15T12:00:00Z");
const base: ApiKeyFormInput = { name: "Site", access: "all", agentIds: [], workflowIds: [], origins: "", expiresAt: null };
const build = (over: Partial<ApiKeyFormInput>) => buildApiKeyCreate({ ...base, ...over }, NOW);

describe("create body: scopes and allowlists", () => {
  it("all access sends only the name: no allowlist keys at all", () => {
    expect(build({})).toEqual({ ok: true, body: { name: "Site" } });
  });
  it("picked access always sends BOTH lists, an empty list for the unpicked kind (fail closed)", () => {
    expect(build({ access: "some", agentIds: ["a1"] })).toEqual({ ok: true, body: { name: "Site", agent_ids: ["a1"], workflow_ids: [] } });
    expect(build({ access: "some", workflowIds: ["w1"] })).toEqual({ ok: true, body: { name: "Site", agent_ids: [], workflow_ids: ["w1"] } });
  });
  it("picked access with nothing picked is refused, never sent as 'all'", () => {
    const result = build({ access: "some" });
    expect(result.ok).toBe(false);
  });
  it("a blank name is refused", () => {
    expect(build({ name: "   " }).ok).toBe(false);
  });
  it("trims the name and does not alias the picked arrays", () => {
    const agentIds = ["a1"];
    const result = build({ name: "  Site  ", access: "some", agentIds });
    expect(result.ok && result.body.name).toBe("Site");
    agentIds.push("a2");
    expect(result.ok && result.body.agent_ids).toEqual(["a1"]);
  });
  it("origins: split, deduplicated, only with a value; a bad origin refuses the whole form", () => {
    const ok = build({ origins: "https://a.example.com\nhttps://a.example.com, http://localhost:3000" });
    expect(ok.ok && ok.body.allowed_origins).toEqual(["https://a.example.com", "http://localhost:3000"]);
    for (const bad of ["a.example.com", "https://a.example.com/path", "ftp://x.com", "*", "https://x.com?q=1"]) {
      expect(build({ origins: bad }).ok, bad).toBe(false);
    }
  });
  it("expiry: future is sent, past or now is refused", () => {
    const future = build({ expiresAt: "2026-12-01T00:00:00Z" });
    expect(future.ok && future.body.expires_at).toBe("2026-12-01T00:00:00Z");
    expect(build({ expiresAt: "2026-11-15T12:00:00Z" }).ok).toBe(false);
    expect(build({ expiresAt: "2020-01-01T00:00:00Z" }).ok).toBe(false);
  });
});

describe("describeAccess is fail-closed in wording", () => {
  it("null/null is everything; an empty list is none, not all", () => {
    expect(describeAccess({ agent_ids: null, workflow_ids: null })).toBe("All agents and workflows");
    expect(describeAccess({ agent_ids: [], workflow_ids: [] })).toBe("0 agents, 0 workflows");
    expect(describeAccess({ agent_ids: ["a"], workflow_ids: null })).toBe("1 agent, 0 workflows");
  });
});

describe("snippets and endpoints", () => {
  const agent = { kind: "agent", id: "agent 1/x" } as const;
  const wf = { kind: "workflow", id: "wf1" } as const;
  it("endpoints carry the tenant-free base, kind and encoded id", () => {
    expect(aguiEndpoint(agent, "https://api.example.com/")).toBe("https://api.example.com/api/v1/ag-ui/agents/agent%201%2Fx");
    expect(aguiEndpoint(wf, "https://api.example.com")).toBe("https://api.example.com/api/v1/ag-ui/workflows/wf1");
    expect(aguiThreadUrl(wf, "https://h")).toBe("https://h/api/v1/ag-ui/workflows/wf1/threads/{threadId}");
    expect(aguiAttachUrl(agent, "https://h")).toBe("https://h/api/v1/ag-ui/agents/agent%201%2Fx/threads/{threadId}/runs/{runId}/stream");
  });
  it.each([agent, wf])("never contain a key, only the environment variable placeholder (%j)", (target) => {
    for (const text of [copilotKitSnippet(target, "https://h"), curlSnippet(target, "https://h")]) {
      expect(text).not.toMatch(/pos_[0-9a-f]/i);
      expect(text).toContain(API_KEY_ENV_VAR);
      expect(text).toContain(aguiEndpoint(target, "https://h"));
      expect(text).not.toMatch(/dev:[^\s"']+:/);
    }
    expect(curlSnippet(target, "https://h")).toContain("$" + API_KEY_ENV_VAR);
    expect(copilotKitSnippet(target, "https://h")).toContain("process.env." + API_KEY_ENV_VAR);
  });
  it("the curl body is valid JSON", () => {
    const line = curlSnippet(agent, "https://h").split("\n").find((l) => l.startsWith("  -d '"))!;
    expect(() => JSON.parse(line.slice(6, -1))).not.toThrow();
  });
});

describe("client", () => {
  const created: ApiKeyCreated = {
    id: "k1", name: "n", prefix: "pos_k1_", status: "active", scopes: ["agui:run"], agent_ids: null, workflow_ids: null,
    allowed_origins: null, created_by: "u", created_at: "2026-11-15T00:00:00Z", expires_at: null, revoked_at: null, last_used_at: null,
    key: "pos_k1_secretsecret",
  };
  beforeEach(() => vi.stubEnv("NEXT_PUBLIC_EAP_DEV_TOKEN", "Bearer dev:t1:u1"));
  afterEach(() => {
    vi.unstubAllEnvs();
    vi.unstubAllGlobals();
  });
  it("withoutSecret drops key and leaves no trace of it", () => {
    const row = withoutSecret(created);
    expect("key" in row).toBe(false);
    expect(JSON.stringify(row)).not.toContain("secretsecret");
    expect(created.key).toBe("pos_k1_secretsecret");
  });
  it("create and list use no-store; revoke encodes the id", async () => {
    const fetchMock = vi.fn(async () => new Response(JSON.stringify({ items: [] }), { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);
    await apiKeys.list();
    fetchMock.mockImplementationOnce(async () => new Response(null, { status: 204 }));
    await apiKeys.revoke("a/b");
    for (const call of fetchMock.mock.calls as unknown as [string, RequestInit][]) expect(call[1].cache).toBe("no-store");
    expect((fetchMock.mock.calls[1] as unknown as [string])[0]).toMatch(/\/api\/v1\/api-keys\/a%2Fb$/);
  });
});
