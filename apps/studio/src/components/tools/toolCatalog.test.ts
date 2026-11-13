import { describe, expect, it } from "vitest";
import type { Agent, AgentSpec } from "@/lib/api";
import { mcpUsageByTool } from "@/components/mcp/mcpUsage";
import type { ToolDefinition } from "@/lib/tools";
import { routes } from "@/lib/routes";
import { buildToolRows, findToolRow, sourceLabel, specToolKeys, toolKey, toolUsageByKey } from "./toolCatalog";

function spec(tools: string[], mcp: Array<[string, string]> = []): AgentSpec {
  return {
    spec_version: 1,
    name: "x",
    instructions: "",
    model: { provider: "openai", name: "gpt" },
    tools,
    ...(mcp.length ? { mcp_tools: mcp.map(([server, tool]) => ({ server, tool })) } : {}),
  };
}

const catalog: ToolDefinition[] = [
  { id: "tickets.create", title: "Create ticket", description: "Creates", side_effect: true, external_idempotent: false, input_schema: { type: "object" }, allowed: true },
  { id: "clock.now", title: "Current time", description: "Time", side_effect: false, external_idempotent: true, input_schema: { type: "object" }, allowed: true },
];

describe("tool catalog rows", () => {
  it("lists built-in tools in catalog order, then MCP tools referenced by drafts or latest versions, without duplicates", () => {
    const agents: Agent[] = [
      { id: "a1", name: "One", draft_spec: spec([], [["search", "query"], ["docs", "read"]]), latest_version: 1 },
      { id: "a2", name: "Two", draft_spec: spec([], [["docs", "read"]]), latest_version: null },
    ];
    const latest = new Map([["a1", spec([], [["docs", "write"]])]]);
    const rows = buildToolRows(catalog, agents, latest);
    expect(rows.map((row) => [row.name, sourceLabel(row.source)])).toEqual([
      ["tickets.create", "Built-in"],
      ["clock.now", "Built-in"],
      ["read", "docs"],
      ["write", "docs"],
      ["query", "search"],
    ]);
    expect(rows[2].definition).toBeNull();
    expect(findToolRow(rows, "read", "docs")?.key).toBe(toolKey("read", "docs"));
    expect(findToolRow(rows, "read", null)).toBeNull();
    expect(findToolRow(rows, "clock.now", null)?.title).toBe("Current time");
  });

  it("keeps a built-in id and an MCP tool of the same name apart", () => {
    expect(toolKey("clock.now")).not.toBe(toolKey("clock.now", "srv"));
    expect(specToolKeys(spec(["clock.now"], [["srv", "clock.now"]]))).toEqual([toolKey("clock.now"), toolKey("clock.now", "srv")]);
  });

  it("counts the agents using each tool, by draft or latest published version", () => {
    const agents: Agent[] = [
      { id: "a1", name: "Zed", draft_spec: spec(["clock.now"]), latest_version: 2 },
      { id: "a2", name: "Alpha", draft_spec: spec([]), latest_version: 1 },
      { id: "a3", name: "Mid", draft_spec: spec([], [["docs", "read"]]), latest_version: null },
    ];
    const latest = new Map([["a1", spec(["clock.now"])], ["a2", spec(["clock.now", "tickets.create"])]]);
    const usage = toolUsageByKey(agents, latest);
    expect(usage.get(toolKey("clock.now"))).toEqual([
      { agentId: "a2", agentName: "Alpha", draft: false, published: 1 },
      { agentId: "a1", agentName: "Zed", draft: true, published: 2 },
    ]);
    expect(usage.get(toolKey("tickets.create"))?.map((u) => u.agentName)).toEqual(["Alpha"]);
    expect(usage.get(toolKey("read", "docs"))?.map((u) => u.agentName)).toEqual(["Mid"]);
    // The MCP page's per-tool usage agrees with the Tools page's.
    expect(mcpUsageByTool(agents, latest).get(JSON.stringify(["docs", "read"]))).toEqual(usage.get(toolKey("read", "docs")));
  });
});

describe("tool routes", () => {
  it("addresses a tool's Sheet by ?tool=, with &server= for an MCP tool and &tab=run for the Run tab", () => {
    expect(routes.tools()).toBe("/tools");
    expect(routes.tool("clock.now")).toBe("/tools?tool=clock.now");
    expect(routes.tool("clock.now", { tab: "overview" })).toBe("/tools?tool=clock.now");
    expect(routes.tool("clock.now", { tab: "run" })).toBe("/tools?tool=clock.now&tab=run");
    expect(routes.tool("read me", { server: "docs/1" })).toBe("/tools?tool=read+me&server=docs%2F1");
  });
});
