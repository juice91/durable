import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { api, type Agent, type AgentSpec, type AgentVersion } from "./api";
import {
  AGENT_SPECS_TIMEOUT_MS,
  agentUsageByKey,
  latestSpecMap,
  loadAgentLatestSpecs,
  useAgentLatestSpecs,
} from "./agentUsage";

vi.mock("./api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("./api")>();
  return { ...actual, api: { ...actual.api, agents: { ...actual.api.agents, list: vi.fn() }, versions: { ...actual.api.versions, get: vi.fn() } } };
});

function spec(tools: string[]): AgentSpec {
  return { spec_version: 1, name: "x", instructions: "", model: { provider: "openai", name: "gpt" }, tools };
}

function agent(id: string, name: string, draft: string[], latest: number | null): Agent {
  return { id, name, draft_spec: spec(draft), latest_version: latest };
}

function version(agentId: string, n: number, tools: string[]): AgentVersion {
  return { agent_id: agentId, version: n, spec: spec(tools), spec_hash: "h", created_at: "", created_by: "" };
}

beforeEach(() => {
  vi.mocked(api.agents.list).mockReset();
  vi.mocked(api.versions.get).mockReset();
});

afterEach(() => {
  vi.useRealTimers();
});

describe("loadAgentLatestSpecs", () => {
  it("reads the latest version of published agents only, keeps the order, and marks unreadable versions failed", async () => {
    vi.mocked(api.versions.get).mockImplementation(async (id: string) => {
      if (id === "p1") return version("p1", 2, ["clock.now"]);
      throw new Error("404");
    });
    const agents = [agent("p1", "One", [], 2), agent("p2", "Two", [], 1), agent("d1", "Draft", ["clock.now"], null)];
    const specs = await loadAgentLatestSpecs(agents);
    expect(api.versions.get).toHaveBeenCalledTimes(2);
    expect(specs.map((item) => item.agent.id)).toEqual(["p1", "p2", "d1"]);
    expect(specs[0]).toMatchObject({ latest: spec(["clock.now"]) });
    expect(specs[0].failed).toBeUndefined();
    expect(specs[1]).toMatchObject({ latest: null, failed: true });
    expect(specs[2]).toMatchObject({ latest: null, draft: spec(["clock.now"]) });
    expect(specs[2].failed).toBeUndefined();
    expect([...latestSpecMap(specs).keys()]).toEqual(["p1"]);
  });

  it("gives up on a version read after the timeout and marks it failed", async () => {
    vi.useFakeTimers();
    vi.mocked(api.versions.get).mockImplementation(() => new Promise(() => undefined));
    const pending = loadAgentLatestSpecs([agent("p1", "One", [], 1)]);
    await vi.advanceTimersByTimeAsync(AGENT_SPECS_TIMEOUT_MS + 10);
    expect((await pending)[0]).toMatchObject({ latest: null, failed: true });
  });
});

describe("agentUsageByKey", () => {
  it("groups agents by referenced key from the draft and latest version, sorted by name", () => {
    const agents = [agent("a1", "Zed", ["x"], null), agent("a2", "Alpha", [], 3), agent("a3", "Mid", ["x"], 2)];
    const latest = new Map<string, AgentSpec>([["a2", spec(["x", "y"])], ["a3", spec([])]]);
    const byKey = agentUsageByKey(agents, latest, (s) => s?.tools ?? []);
    expect(byKey.get("x")).toEqual([
      { agentId: "a2", agentName: "Alpha", draft: false, published: 3 },
      { agentId: "a3", agentName: "Mid", draft: true, published: null },
      { agentId: "a1", agentName: "Zed", draft: true, published: null },
    ]);
    expect(byKey.get("y")?.map((u) => u.agentId)).toEqual(["a2"]);
  });
});

describe("useAgentLatestSpecs", () => {
  it("loads agents and versions, flags a partial scan, and reloads", async () => {
    vi.mocked(api.agents.list).mockResolvedValue([agent("p1", "One", [], 1), agent("p2", "Two", [], 1)]);
    vi.mocked(api.versions.get).mockImplementation(async (id: string) => {
      if (id === "p1") return version("p1", 1, ["clock.now"]);
      throw new Error("500");
    });
    const { result } = renderHook(() => useAgentLatestSpecs());
    expect(result.current.status).toBe("loading");
    await waitFor(() => expect(result.current.status).toBe("ready"));
    expect(result.current.agents).toHaveLength(2);
    expect(result.current.partial).toBe(true);
    expect(result.current.latestSpecs.get("p1")).toEqual(spec(["clock.now"]));

    act(() => result.current.reload());
    expect(result.current.status).toBe("loading");
    await waitFor(() => expect(result.current.status).toBe("ready"));
    expect(api.agents.list).toHaveBeenCalledTimes(2);
  });

  it("recheck keeps the ready result on screen while it runs; refreshIfStale skips a fresh result", async () => {
    vi.mocked(api.agents.list).mockResolvedValue([agent("d1", "Draft", [], null)]);
    const { result } = renderHook(() => useAgentLatestSpecs());
    await waitFor(() => expect(result.current.status).toBe("ready"));
    act(() => result.current.refreshIfStale());
    expect(api.agents.list).toHaveBeenCalledTimes(1);

    let release: (agents: Agent[]) => void = () => undefined;
    vi.mocked(api.agents.list).mockImplementationOnce(() => new Promise((resolve) => { release = resolve; }));
    act(() => result.current.recheck());
    expect(result.current.status).toBe("ready");
    expect(result.current.refreshing).toBe(true);
    await act(async () => release([]));
    await waitFor(() => expect(result.current.refreshing).toBe(false));
    expect(result.current.agents).toEqual([]);
  });

  it("reports an agents list failure as error with nothing loaded", async () => {
    vi.mocked(api.agents.list).mockRejectedValue(new Error("down"));
    const { result } = renderHook(() => useAgentLatestSpecs());
    await waitFor(() => expect(result.current.status).toBe("error"));
    expect(result.current.agents).toEqual([]);
    expect(result.current.specs).toEqual([]);
  });
});
