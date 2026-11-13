import { describe, expect, it, vi } from "vitest";
import type { Agent, AgentSpec } from "@/lib/api";
import { describeUsage, mapSettledWithLimit, mcpServerUsage, mcpUsageByServer, McpUsageTimeoutError, usageCountLabel, withTimeout } from "./mcpUsage";

function spec(servers: string[]): AgentSpec {
  return {
    spec_version: 1,
    name: "x",
    instructions: "",
    model: { provider: "openai", name: "gpt" },
    tools: [],
    ...(servers.length ? { mcp_tools: servers.map((server) => ({ server, tool: "t" })) } : {}),
  };
}

function agent(id: string, name: string, draft: string[], latest: number | null): Agent {
  return { id, name, draft_spec: spec(draft), latest_version: latest };
}

describe("mcpServerUsage", () => {
  it("finds agents whose draft or latest published version uses the server, by server name, sorted", () => {
    const agents = [
      agent("a1", "Zed", ["docs"], null),
      agent("a2", "Alpha", [], 3),
      agent("a3", "Mid", ["docs"], 2),
      agent("a4", "Other", ["search"], 1),
    ];
    const latest = new Map<string, AgentSpec>([
      ["a2", spec(["docs"])],
      ["a3", spec([])],
      ["a4", spec(["search"])],
    ]);
    expect(mcpServerUsage(agents, latest, "docs")).toEqual([
      { agentId: "a2", agentName: "Alpha", draft: false, published: 3 },
      { agentId: "a3", agentName: "Mid", draft: true, published: null },
      { agentId: "a1", agentName: "Zed", draft: true, published: null },
    ]);
    expect(mcpServerUsage(agents, latest, "nope")).toEqual([]);
  });

  it("judges an agent without a readable latest spec by its draft only", () => {
    const agents = [agent("a1", "A", [], 4), agent("a2", "B", ["docs"], 4)];
    expect(mcpServerUsage(agents, new Map(), "docs")).toEqual([{ agentId: "a2", agentName: "B", draft: true, published: null }]);
  });

  it("describes where the server is used", () => {
    expect(describeUsage({ agentId: "a", agentName: "A", draft: true, published: 2 })).toBe("draft, v2");
    expect(describeUsage({ agentId: "a", agentName: "A", draft: true, published: null })).toBe("draft");
    expect(describeUsage({ agentId: "a", agentName: "A", draft: false, published: 7 })).toBe("v7");
  });
});

describe("mcpUsageByServer", () => {
  it("maps every referenced server name in one pass", () => {
    const map = mcpUsageByServer([agent("a1", "A", ["docs", "search"], null), agent("a2", "B", ["docs"], null)], new Map());
    expect([...map.keys()].sort()).toEqual(["docs", "search"]);
    expect(map.get("docs")?.map((u) => u.agentId)).toEqual(["a1", "a2"]);
  });
});

describe("mapSettledWithLimit", () => {
  it("keeps at most `limit` tasks in flight and keeps the input order", async () => {
    let inFlight = 0;
    let peak = 0;
    const results = await mapSettledWithLimit([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], 3, async (n) => {
      inFlight += 1;
      peak = Math.max(peak, inFlight);
      await new Promise((resolve) => setTimeout(resolve, 1));
      inFlight -= 1;
      if (n === 4) throw new Error("four");
      return n * 2;
    });
    expect(peak).toBe(3);
    expect(results[0]).toEqual({ status: "fulfilled", value: 2 });
    expect(results[3].status).toBe("rejected");
    expect(results).toHaveLength(10);
  });
});

describe("usageCountLabel", () => {
  it("adds + when the check was partial", () => {
    expect(usageCountLabel(1, false)).toBe("1 agent");
    expect(usageCountLabel(2, false)).toBe("2 agents");
    expect(usageCountLabel(0, true)).toBe("0+ agents");
    expect(usageCountLabel(1, true)).toBe("1+ agents");
  });
});

describe("withTimeout", () => {
  it("aborts the task and rejects with McpUsageTimeoutError after the limit", async () => {
    vi.useFakeTimers();
    try {
      let signal: AbortSignal | null = null;
      const pending = withTimeout((s) => {
        signal = s;
        return new Promise<never>(() => undefined);
      }, 1000);
      const assertion = expect(pending).rejects.toBeInstanceOf(McpUsageTimeoutError);
      await vi.advanceTimersByTimeAsync(1001);
      await assertion;
      expect(signal!.aborted).toBe(true);
    } finally {
      vi.useRealTimers();
    }
  });

  it("passes a result through and does not abort", async () => {
    let signal: AbortSignal | null = null;
    await expect(withTimeout(async (s) => { signal = s; return 7; }, 1000)).resolves.toBe(7);
    expect(signal!.aborted).toBe(false);
  });
});
