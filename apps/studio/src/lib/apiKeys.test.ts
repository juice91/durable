import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  aguiAttachUrl,
  aguiEndpoint,
  aguiThreadUrl,
  API_KEY_ENV_VAR,
  apiKeys,
  buildApiKeyCreate,
  COPILOTKIT_INSTALL,
  copilotKitSnippet,
  curlSnippet,
  describeAccess,
  parseOrigins,
  withoutSecret,
  type ApiKeyCreated,
} from "./apiKeys";

const BASE = "https://api.example.com/";

const created: ApiKeyCreated = {
  id: "abc",
  name: "Site",
  prefix: "pos_abc_",
  status: "active",
  scopes: ["agui:run"],
  agent_ids: null,
  workflow_ids: null,
  allowed_origins: null,
  created_by: "u1",
  created_at: "2026-11-15T00:00:00Z",
  expires_at: null,
  revoked_at: null,
  last_used_at: null,
  key: "pos_abc_supersecret",
};

describe("api keys client", () => {
  let fetchMock: ReturnType<typeof vi.fn>;
  beforeEach(() => {
    vi.stubEnv("NEXT_PUBLIC_EAP_DEV_TOKEN", "Bearer dev:t1:u1");
    fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
  });
  afterEach(() => {
    vi.unstubAllEnvs();
    vi.unstubAllGlobals();
  });

  it("lists, creates (no-store) and revokes on /api/v1/api-keys", async () => {
    fetchMock.mockResolvedValueOnce(new Response(JSON.stringify({ items: [withoutSecret(created)] }), { status: 200 }));
    expect(await apiKeys.list()).toEqual([withoutSecret(created)]);
    expect(fetchMock.mock.calls[0][0]).toMatch(/\/api\/v1\/api-keys$/);

    fetchMock.mockResolvedValueOnce(new Response(JSON.stringify(created), { status: 201 }));
    expect((await apiKeys.create({ name: "Site" })).key).toBe(created.key);
    const [, init] = fetchMock.mock.calls[1];
    expect(init.method).toBe("POST");
    expect(init.cache).toBe("no-store");
    expect(JSON.parse(init.body)).toEqual({ name: "Site" });

    fetchMock.mockResolvedValueOnce(new Response(null, { status: 204 }));
    await apiKeys.revoke("a/b");
    expect(fetchMock.mock.calls[2][0]).toMatch(/\/api\/v1\/api-keys\/a%2Fb$/);
    expect(fetchMock.mock.calls[2][1].method).toBe("DELETE");
  });

  it("throws the API's message", async () => {
    fetchMock.mockResolvedValueOnce(new Response(JSON.stringify({ detail: "API key not found" }), { status: 404 }));
    await expect(apiKeys.revoke("x")).rejects.toMatchObject({ status: 404, message: "API key not found" });
  });
});

describe("withoutSecret", () => {
  it("drops the plaintext", () => {
    const listed = withoutSecret(created);
    expect(listed).not.toHaveProperty("key");
    expect(JSON.stringify(listed)).not.toContain("supersecret");
  });
});

describe("buildApiKeyCreate", () => {
  const base = { name: " Site ", access: "all" as const, agentIds: [], workflowIds: [], origins: "", expiresAt: null };
  const now = new Date("2026-11-15T00:00:00Z");

  it("sends only the name for a tenant-wide key", () => {
    expect(buildApiKeyCreate(base, now)).toEqual({ ok: true, body: { name: "Site" } });
  });

  it("restricts both lists when access is picked", () => {
    expect(buildApiKeyCreate({ ...base, access: "some", agentIds: ["a1"] }, now)).toEqual({
      ok: true,
      body: { name: "Site", agent_ids: ["a1"], workflow_ids: [] },
    });
    expect(buildApiKeyCreate({ ...base, access: "some" }, now).ok).toBe(false);
  });

  it("validates the name, origins and expiry", () => {
    expect(buildApiKeyCreate({ ...base, name: "  " }, now).ok).toBe(false);
    // Origins are the server's to judge: sent as typed.
    expect(buildApiKeyCreate({ ...base, origins: "app.example.com" }, now)).toEqual({ ok: true, body: { name: "Site", allowed_origins: ["app.example.com"] } });
    expect(buildApiKeyCreate({ ...base, origins: "https://a.com\nhttps://a.com, http://b.local:3000" }, now)).toEqual({
      ok: true,
      body: { name: "Site", allowed_origins: ["https://a.com", "http://b.local:3000"] },
    });
    expect(buildApiKeyCreate({ ...base, expiresAt: "2026-11-14T00:00:00.000Z" }, now).ok).toBe(false);
    expect(buildApiKeyCreate({ ...base, expiresAt: "2026-12-01T00:00:00.000Z" }, now)).toEqual({
      ok: true,
      body: { name: "Site", expires_at: "2026-12-01T00:00:00.000Z" },
    });
  });

  it("parseOrigins splits, trims and dedupes", () => {
    expect(parseOrigins(" https://a.com ,\n\nhttps://a.com\nhttps://b.com")).toEqual(["https://a.com", "https://b.com"]);
  });
});

describe("describeAccess", () => {
  it("names tenant-wide keys and counts allowlists", () => {
    expect(describeAccess({ agent_ids: null, workflow_ids: null })).toBe("All agents and workflows");
    expect(describeAccess({ agent_ids: ["a"], workflow_ids: [] })).toBe("1 agent, 0 workflows");
  });
});

describe("connect snippets", () => {
  const agent = { kind: "agent" as const, id: "ag/1" };
  const workflow = { kind: "workflow" as const, id: "wf1" };

  it("builds the endpoint URLs", () => {
    expect(aguiEndpoint(agent, BASE)).toBe("https://api.example.com/api/v1/ag-ui/agents/ag%2F1");
    expect(aguiEndpoint(workflow, BASE)).toBe("https://api.example.com/api/v1/ag-ui/workflows/wf1");
    expect(aguiThreadUrl(agent, BASE)).toBe("https://api.example.com/api/v1/ag-ui/agents/ag%2F1/threads/{threadId}");
    expect(aguiAttachUrl(agent, BASE)).toBe("https://api.example.com/api/v1/ag-ui/agents/ag%2F1/threads/{threadId}/runs/{runId}/stream");
  });

  it("CopilotKit and curl snippets contain the URL and the env var, never a key", () => {
    for (const target of [agent, workflow]) {
      for (const snippet of [copilotKitSnippet(target, BASE), curlSnippet(target, BASE)]) {
        expect(snippet).toContain(aguiEndpoint(target, BASE));
        expect(snippet).toContain(API_KEY_ENV_VAR);
        expect(snippet).not.toMatch(/pos_[0-9a-f]/);
      }
    }
    expect(copilotKitSnippet(agent, BASE)).toContain("new HttpAgent(");
    // AGUI.16: the install line pins the @ag-ui/client copy @copilotkit/runtime 1.83.0 depends on.
    expect(copilotKitSnippet(agent, BASE).split("\n")[0]).toBe(`// ${COPILOTKIT_INSTALL}`);
    expect(COPILOTKIT_INSTALL).toContain("@ag-ui/client@1.0.2");
    expect(copilotKitSnippet(agent, BASE)).toContain("useSingleEndpoint");
    expect(curlSnippet(workflow, BASE)).toContain('"pydanticos":{"input":{}}');
  });
});
