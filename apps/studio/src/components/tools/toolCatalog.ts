// The Tools page's rows: the built-in catalog (`GET /tools`) plus the MCP tools that agent specs
// reference (`spec.mcp_tools`), each with the agents that use it. MCP tools have no catalog entry:
// the API lists a server's tools only through a live connection test, so their rows come from the
// specs and their Sheet has no Run tab.
import type { Agent, AgentSpec } from "@/lib/api";
import { agentUsageByKey, type AgentUsage } from "@/lib/agentUsage";
import type { ToolDefinition } from "@/lib/tools";

export type ToolSource = { kind: "builtin" } | { kind: "mcp"; server: string };

export interface ToolRow {
  /** Unique across sources: the built-in id, or the MCP server and tool name. */
  key: string;
  /** The tool's name: the built-in id, or the MCP tool name. This is `?tool=`. */
  name: string;
  title: string;
  description: string;
  source: ToolSource;
  /** The catalog entry (built-in tools only). */
  definition: ToolDefinition | null;
}

/** Row key of a built-in tool or of an MCP server's tool. */
export function toolKey(name: string, server?: string | null): string {
  return server ? `mcp:${JSON.stringify([server, name])}` : `builtin:${name}`;
}

/** Every tool an agent spec references, as row keys. */
export function specToolKeys(spec: AgentSpec | null | undefined): string[] {
  if (!spec) return [];
  return [
    ...(spec.tools ?? []).map((id) => toolKey(id)),
    ...(spec.mcp_tools ?? []).map((ref) => toolKey(ref.tool, ref.server)),
  ];
}

/** Row key → the agents whose draft or latest published version uses the tool. */
export function toolUsageByKey(agents: readonly Agent[], latestSpecs: ReadonlyMap<string, AgentSpec>): Map<string, AgentUsage[]> {
  return agentUsageByKey(agents, latestSpecs, specToolKeys);
}

/**
 * The page's rows: built-in tools in catalog order, then the MCP tools referenced by any agent spec
 * (draft or latest version), sorted by server then tool name.
 */
export function buildToolRows(catalog: readonly ToolDefinition[], agents: readonly Agent[], latestSpecs: ReadonlyMap<string, AgentSpec>): ToolRow[] {
  const rows: ToolRow[] = catalog.map((definition) => ({
    key: toolKey(definition.id),
    name: definition.id,
    title: definition.title,
    description: definition.description,
    source: { kind: "builtin" },
    definition,
  }));
  const mcp = new Map<string, { server: string; tool: string }>();
  const collect = (spec: AgentSpec | null | undefined) => {
    for (const ref of spec?.mcp_tools ?? []) mcp.set(toolKey(ref.tool, ref.server), { server: ref.server, tool: ref.tool });
  };
  for (const agent of agents) {
    collect(agent.draft_spec);
    collect(latestSpecs.get(agent.id));
  }
  const referenced = [...mcp.entries()].sort(([, a], [, b]) => a.server.localeCompare(b.server) || a.tool.localeCompare(b.tool));
  for (const [key, { server, tool }] of referenced) {
    rows.push({ key, name: tool, title: tool, description: "", source: { kind: "mcp", server }, definition: null });
  }
  return rows;
}

/** The row `?tool=<name>[&server=<server>]` points at, if any. */
export function findToolRow(rows: readonly ToolRow[], name: string | null, server: string | null): ToolRow | null {
  if (!name) return null;
  return rows.find((row) => row.key === toolKey(name, server)) ?? null;
}

/** "Built-in" or the MCP server's name. */
export function sourceLabel(source: ToolSource): string {
  return source.kind === "builtin" ? "Built-in" : source.server;
}
