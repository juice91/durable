import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { AgentArchiveBlocked, ApiError, api } from "./api";

// Review blocker: POST /agents/{id}/archive's 409 carries the published workflows that run the agent.

beforeEach(() => vi.stubEnv("NEXT_PUBLIC_EAP_DEV_TOKEN", "Bearer test"));
afterEach(() => {
  vi.unstubAllGlobals();
  vi.unstubAllEnvs();
});

function stubFetch(status: number, body: unknown) {
  const fetchMock = vi.fn(async (_url: string | URL | Request, _init?: RequestInit) => new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } }));
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

describe("api.agents.archive", () => {
  it("throws AgentArchiveBlocked with the workflows on a 409 that lists them", async () => {
    const workflows = [{ workflow_id: "w1", workflow_name: "Triage", versions: [1], latest_version: 1 }];
    stubFetch(409, { detail: "agent is a step of 1 published workflow(s)", workflows });
    const error = await api.agents.archive("a1").catch((e: unknown) => e);
    expect(error).toBeInstanceOf(AgentArchiveBlocked);
    expect((error as AgentArchiveBlocked).workflows).toEqual(workflows);
    expect((error as AgentArchiveBlocked).status).toBe(409);
  });

  it("sends force=true only when asked, and other errors stay ApiErrors", async () => {
    const fetchMock = stubFetch(200, { id: "a1", archived: true });
    await api.agents.archive("a1", { force: true });
    await api.agents.archive("a1");
    expect(String(fetchMock.mock.calls[0][0])).toMatch(/\/agents\/a1\/archive\?force=true$/);
    expect(String(fetchMock.mock.calls[1][0])).toMatch(/\/agents\/a1\/archive$/);
    stubFetch(404, { detail: "Agent not found" });
    const error = await api.agents.archive("a1").catch((e: unknown) => e);
    expect(error).toBeInstanceOf(ApiError);
    expect(error).not.toBeInstanceOf(AgentArchiveBlocked);
  });
});
