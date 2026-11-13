"use client";

// Which agents use an MCP server. Agents reference MCP tools by the server's NAME
// (`spec.mcp_tools[].server`), in the draft and in published versions. "Uses" means the draft or the
// latest published version has at least one tool from the server: those are the agents that stop
// publishing (draft) or running (latest) when the server goes away.
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api, type Agent, type AgentSpec } from "@/lib/api";

export interface McpAgentUsage {
  agentId: string;
  agentName: string;
  /** The draft spec uses the server. */
  draft: boolean;
  /** The latest published version number when that version uses the server, else null. */
  published: number | null;
}

/** A loaded result is reused for this long before a dialog open re-checks it. */
export const MCP_USAGE_FRESH_MS = 60_000;
/** At most this many `GET /agents/{id}/versions/{n}` requests run at once. */
export const MCP_USAGE_CONCURRENCY = 6;
/** Each usage request gives up after this long: a version read then counts as unreadable (partial),
 * the agents list as an error, so a confirm waiting on the check is never disabled forever. */
export const MCP_USAGE_TIMEOUT_MS = 15_000;

export class McpUsageTimeoutError extends Error {
  constructor(public readonly ms: number) {
    super(`usage request timed out after ${ms} ms`);
    this.name = "McpUsageTimeoutError";
  }
}

/** Runs `task` with an abort signal; aborts it and rejects with McpUsageTimeoutError after `ms`. */
export function withTimeout<T>(task: (signal: AbortSignal) => Promise<T>, ms: number = MCP_USAGE_TIMEOUT_MS): Promise<T> {
  const controller = new AbortController();
  return new Promise<T>((resolve, reject) => {
    const timer = setTimeout(() => {
      controller.abort();
      reject(new McpUsageTimeoutError(ms));
    }, ms);
    task(controller.signal).then(
      (value) => {
        clearTimeout(timer);
        resolve(value);
      },
      (reason: unknown) => {
        clearTimeout(timer);
        reject(reason);
      },
    );
  });
}

function serverNames(spec: AgentSpec | null | undefined): Set<string> {
  return new Set((spec?.mcp_tools ?? []).map((ref) => ref.server));
}

/**
 * Server name → the agents that use it, sorted by agent name, in one pass over the agents.
 * `latestSpecs` maps an agent id to its latest published spec; agents missing from it are judged by
 * their draft alone.
 */
export function mcpUsageByServer(agents: Agent[], latestSpecs: ReadonlyMap<string, AgentSpec>): Map<string, McpAgentUsage[]> {
  const byServer = new Map<string, McpAgentUsage[]>();
  for (const agent of agents) {
    const draft = serverNames(agent.draft_spec);
    const latest = agent.latest_version !== null ? serverNames(latestSpecs.get(agent.id)) : new Set<string>();
    for (const name of new Set([...draft, ...latest])) {
      const list = byServer.get(name) ?? [];
      list.push({ agentId: agent.id, agentName: agent.name, draft: draft.has(name), published: latest.has(name) ? agent.latest_version : null });
      byServer.set(name, list);
    }
  }
  for (const list of byServer.values()) list.sort((a, b) => a.agentName.localeCompare(b.agentName));
  return byServer;
}

/** The agents that use `serverName` (see `mcpUsageByServer`). */
export function mcpServerUsage(agents: Agent[], latestSpecs: ReadonlyMap<string, AgentSpec>, serverName: string): McpAgentUsage[] {
  return mcpUsageByServer(agents, latestSpecs).get(serverName) ?? [];
}

/** "draft", "v3" or "draft, v3": where an agent uses the server. */
export function describeUsage(usage: McpAgentUsage): string {
  const parts: string[] = [];
  if (usage.draft) parts.push("draft");
  if (usage.published !== null) parts.push(`v${usage.published}`);
  return parts.join(", ");
}

/** Runs `task` over `items` with at most `limit` in flight; results keep the input order. */
export async function mapSettledWithLimit<T, R>(items: readonly T[], limit: number, task: (item: T) => Promise<R>): Promise<PromiseSettledResult<R>[]> {
  const results: PromiseSettledResult<R>[] = new Array(items.length);
  let next = 0;
  const worker = async () => {
    while (next < items.length) {
      const index = next++;
      try {
        results[index] = { status: "fulfilled", value: await task(items[index]) };
      } catch (reason: unknown) {
        results[index] = { status: "rejected", reason };
      }
    }
  };
  await Promise.all(Array.from({ length: Math.min(Math.max(1, limit), items.length) }, worker));
  return results;
}

export type McpUsageStatus = "loading" | "ready" | "error";

export interface McpUsageState {
  status: McpUsageStatus;
  /** A background re-check (`recheck`) is running; `usageFor` still answers from the previous result. */
  refreshing: boolean;
  /** Some agents' latest versions could not be read; their drafts were still checked. */
  partial: boolean;
  /** The agents using a server (empty until `status` is "ready"). */
  usageFor: (serverName: string) => McpAgentUsage[];
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
 * Loads `GET /agents` and, for each published agent, its latest version (`GET …/versions/{n}`, at most
 * MCP_USAGE_CONCURRENCY at once), then answers "who uses this server". A failed agents list gives
 * status "error" (callers say they could not check); a failed version read only marks it `partial`.
 */
export function useMcpUsage(): McpUsageState {
  const [status, setStatus] = useState<McpUsageStatus>("loading");
  const [refreshing, setRefreshing] = useState(false);
  const [partial, setPartial] = useState(false);
  const [byServer, setByServer] = useState<Map<string, McpAgentUsage[]>>(new Map());
  const generation = useRef(0);
  const loadedAt = useRef<number | null>(null);
  const statusRef = useRef<McpUsageStatus>("loading");
  statusRef.current = status;

  /** `background`: keep the current result and status, and flag `refreshing` instead. */
  const load = useCallback(async (background = false) => {
    const current = ++generation.current;
    if (background) setRefreshing(true);
    else setStatus("loading");
    try {
      const list = await withTimeout((signal) => api.agents.list({ signal }));
      const published = list.filter((agent): agent is Agent & { latest_version: number } => agent.latest_version !== null);
      const results = await mapSettledWithLimit(published, MCP_USAGE_CONCURRENCY, (agent) =>
        withTimeout((signal) => api.versions.get(agent.id, agent.latest_version, { signal })),
      );
      if (current !== generation.current) return;
      const specs = new Map<string, AgentSpec>();
      let missing = false;
      results.forEach((result, index) => {
        if (result.status === "fulfilled" && result.value?.spec) specs.set(published[index].id, result.value.spec);
        else missing = true;
      });
      setByServer(mcpUsageByServer(list, specs));
      setPartial(missing);
      setStatus("ready");
      loadedAt.current = Date.now();
    } catch {
      if (current !== generation.current) return;
      setByServer(new Map());
      setPartial(false);
      setStatus("error");
      loadedAt.current = null;
    } finally {
      if (current === generation.current) setRefreshing(false);
    }
  }, []);

  useEffect(() => {
    void load();
    return () => {
      generation.current += 1;
    };
  }, [load]);

  const usageFor = useCallback((serverName: string) => (status === "ready" ? byServer.get(serverName) ?? NO_USAGE : NO_USAGE), [byServer, status]);
  const reload = useCallback(() => void load(), [load]);
  const refreshIfStale = useCallback(() => {
    if (statusRef.current === "loading") return;
    if (loadedAt.current !== null && Date.now() - loadedAt.current < MCP_USAGE_FRESH_MS) return;
    void load();
  }, [load]);
  // A ready result stays on screen while the re-check runs; otherwise this is a plain load.
  const recheck = useCallback(() => void load(statusRef.current === "ready"), [load]);

  return useMemo(
    () => ({ status, refreshing, partial, usageFor, reload, refreshIfStale, recheck }),
    [status, refreshing, partial, usageFor, reload, refreshIfStale, recheck],
  );
}

/** "3 agents", or "3+ agents" when some versions could not be read. */
export function usageCountLabel(count: number, partial: boolean): string {
  return `${count}${partial ? "+" : ""} agent${count === 1 && !partial ? "" : "s"}`;
}
