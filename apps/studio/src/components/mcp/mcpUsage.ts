"use client";

// Which agents use an MCP server. Agents reference MCP tools by the server's NAME
// (`spec.mcp_tools[].server`), in the draft and in published versions. "Uses" means the draft or the
// latest published version has at least one tool from the server: those are the agents that stop
// publishing (draft) or running (latest) when the server goes away.
//
// The agents-and-versions scan and the grouping live in `lib/agentUsage` (shared with Tools and
// Processors); this file only says what an MCP reference is.
import { useCallback, useMemo } from "react";
import type { Agent, AgentSpec } from "@/lib/api";
import {
  AGENT_SPECS_CONCURRENCY,
  AGENT_SPECS_FRESH_MS,
  AGENT_SPECS_TIMEOUT_MS,
  AgentSpecsTimeoutError,
  agentUsageByKey,
  useAgentLatestSpecs,
  type AgentSpecsStatus,
  type AgentUsage,
} from "@/lib/agentUsage";

export { describeUsage, mapSettledWithLimit, usageCountLabel, withTimeout } from "@/lib/agentUsage";

export type McpAgentUsage = AgentUsage;

/** A loaded result is reused for this long before a dialog open re-checks it. */
export const MCP_USAGE_FRESH_MS = AGENT_SPECS_FRESH_MS;
/** At most this many `GET /agents/{id}/versions/{n}` requests run at once. */
export const MCP_USAGE_CONCURRENCY = AGENT_SPECS_CONCURRENCY;
/** Each usage request gives up after this long (see `AGENT_SPECS_TIMEOUT_MS`). */
export const MCP_USAGE_TIMEOUT_MS = AGENT_SPECS_TIMEOUT_MS;
export const McpUsageTimeoutError = AgentSpecsTimeoutError;

function serverNames(spec: AgentSpec | null | undefined): Set<string> {
  return new Set((spec?.mcp_tools ?? []).map((ref) => ref.server));
}

/**
 * Server name → the agents that use it, sorted by agent name, in one pass over the agents.
 * `latestSpecs` maps an agent id to its latest published spec; agents missing from it are judged by
 * their draft alone.
 */
export function mcpUsageByServer(agents: Agent[], latestSpecs: ReadonlyMap<string, AgentSpec>): Map<string, McpAgentUsage[]> {
  return agentUsageByKey(agents, latestSpecs, serverNames);
}

/** The agents that use `serverName` (see `mcpUsageByServer`). */
export function mcpServerUsage(agents: Agent[], latestSpecs: ReadonlyMap<string, AgentSpec>, serverName: string): McpAgentUsage[] {
  return mcpUsageByServer(agents, latestSpecs).get(serverName) ?? [];
}

/** Key of one MCP tool of one server, for `mcpToolUsage`. */
function mcpToolUsageKey(server: string, tool: string): string {
  return JSON.stringify([server, tool]);
}

/** (server, tool) → the agents whose draft or latest version lists that MCP tool. */
export function mcpUsageByTool(agents: Agent[], latestSpecs: ReadonlyMap<string, AgentSpec>): Map<string, McpAgentUsage[]> {
  return agentUsageByKey(agents, latestSpecs, (spec) => (spec?.mcp_tools ?? []).map((ref) => mcpToolUsageKey(ref.server, ref.tool)));
}

export type McpUsageStatus = AgentSpecsStatus;

export interface McpUsageState {
  status: McpUsageStatus;
  /** A background re-check (`recheck`) is running; `usageFor` still answers from the previous result. */
  refreshing: boolean;
  /** Some agents' latest versions could not be read; their drafts were still checked. */
  partial: boolean;
  /** The agents using a server (empty until `status` is "ready"). */
  usageFor: (serverName: string) => McpAgentUsage[];
  /** The agents using one tool of a server (empty until `status` is "ready"). */
  usageForTool: (serverName: string, toolName: string) => McpAgentUsage[];
  /** Load again now. */
  reload: () => void;
  /** Load again only when the last result is older than MCP_USAGE_FRESH_MS (or failed). For the
   * Used-by column and card. */
  refreshIfStale: () => void;
  /** Always re-check, keeping the current result visible meanwhile (`refreshing`). For decisions such
   * as the delete confirm, which must not act on a cached answer. */
  recheck: () => void;
}

const NO_USAGE: McpAgentUsage[] = [];

/**
 * "Who uses this server", from the shared agents-and-latest-versions scan (`useAgentLatestSpecs`).
 * A failed agents list gives status "error" (callers say they could not check); a failed version
 * read only marks it `partial`.
 */
export function useMcpUsage(): McpUsageState {
  const scan = useAgentLatestSpecs();
  const { status, refreshing, partial, agents, latestSpecs, reload, refreshIfStale, recheck } = scan;
  const byServer = useMemo(() => mcpUsageByServer(agents, latestSpecs), [agents, latestSpecs]);
  const byTool = useMemo(() => mcpUsageByTool(agents, latestSpecs), [agents, latestSpecs]);

  const usageFor = useCallback((serverName: string) => (status === "ready" ? byServer.get(serverName) ?? NO_USAGE : NO_USAGE), [byServer, status]);
  const usageForTool = useCallback(
    (serverName: string, toolName: string) => (status === "ready" ? byTool.get(mcpToolUsageKey(serverName, toolName)) ?? NO_USAGE : NO_USAGE),
    [byTool, status],
  );

  return useMemo(
    () => ({ status, refreshing, partial, usageFor, usageForTool, reload, refreshIfStale, recheck }),
    [status, refreshing, partial, usageFor, usageForTool, reload, refreshIfStale, recheck],
  );
}
