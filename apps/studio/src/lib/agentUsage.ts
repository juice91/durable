// "Used by n agents", computed once for every page that shows it (Tools, MCP Servers, Processors).
//
// There is no server-side usage endpoint: the answer comes from `GET /agents` (each agent's draft
// spec) plus, for each published agent, its latest version (`GET /agents/{id}/versions/{n}`). This
// module owns that scan (`loadAgentLatestSpecs`, and the `useAgentLatestSpecs` hook with its
// freshness rules) and the generic "which agents reference this key" grouping (`agentUsageByKey`).
// Each page only says what a spec references (server names, tool keys, processor types).
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api, type Agent, type AgentSpec } from "./api";

/** A loaded scan is reused for this long before `refreshIfStale` loads it again. */
export const AGENT_SPECS_FRESH_MS = 60_000;
/** At most this many `GET /agents/{id}/versions/{n}` requests run at once (the hook's default). */
export const AGENT_SPECS_CONCURRENCY = 6;
/** Each request gives up after this long: a version read then counts as unreadable (`failed`), the
 * agents list as an error, so nothing waiting on the scan is stuck forever. */
export const AGENT_SPECS_TIMEOUT_MS = 15_000;

export class AgentSpecsTimeoutError extends Error {
  constructor(public readonly ms: number) {
    super(`usage request timed out after ${ms} ms`);
    this.name = "AgentSpecsTimeoutError";
  }
}

/** Runs `task` with an abort signal; aborts it and rejects with AgentSpecsTimeoutError after `ms`. */
export function withTimeout<T>(task: (signal: AbortSignal) => Promise<T>, ms: number = AGENT_SPECS_TIMEOUT_MS): Promise<T> {
  const controller = new AbortController();
  return new Promise<T>((resolve, reject) => {
    const timer = setTimeout(() => {
      controller.abort();
      reject(new AgentSpecsTimeoutError(ms));
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

/** One agent with its draft spec and, when published, its latest version's spec. */
export interface AgentLatestSpec {
  agent: Agent;
  draft: AgentSpec;
  /** The latest published version's spec; null for a draft-only agent or an unreadable version. */
  latest: AgentSpec | null;
  /** True when the latest version could not be read: whether it uses something is unknown. */
  failed?: boolean;
}

export interface LoadAgentLatestSpecsOptions {
  concurrency?: number;
  /** Per-request limit; 0 disables it. */
  timeoutMs?: number;
}

/**
 * For each agent (in order), its draft and latest published spec: one version request per published
 * agent, at most `concurrency` at once, each limited to `timeoutMs`. A version that cannot be read is
 * marked `failed`; this never rejects.
 */
export async function loadAgentLatestSpecs(agents: readonly Agent[], { concurrency = AGENT_SPECS_CONCURRENCY, timeoutMs = AGENT_SPECS_TIMEOUT_MS }: LoadAgentLatestSpecsOptions = {}): Promise<AgentLatestSpec[]> {
  const read = (agent: Agent & { latest_version: number }) =>
    timeoutMs > 0
      ? withTimeout((signal) => api.versions.get(agent.id, agent.latest_version, { signal }), timeoutMs)
      : api.versions.get(agent.id, agent.latest_version);
  const results = await mapSettledWithLimit(agents, concurrency, async (agent): Promise<AgentLatestSpec> => {
    if (agent.latest_version === null) return { agent, draft: agent.draft_spec, latest: null };
    const version = await read(agent as Agent & { latest_version: number });
    if (!version?.spec) throw new Error("version without a spec");
    return { agent, draft: agent.draft_spec, latest: version.spec };
  });
  return results.map((result, index) =>
    result.status === "fulfilled" ? result.value : { agent: agents[index], draft: agents[index].draft_spec, latest: null, failed: true },
  );
}

/** Agent id → latest published spec, for the agents whose latest version was read. */
export function latestSpecMap(specs: readonly AgentLatestSpec[]): Map<string, AgentSpec> {
  const map = new Map<string, AgentSpec>();
  for (const item of specs) if (item.latest) map.set(item.agent.id, item.latest);
  return map;
}

// ---------------------------------------------------------------------------------------------
// Who references a key

export interface AgentUsage {
  agentId: string;
  agentName: string;
  /** The draft spec references it. */
  draft: boolean;
  /** The latest published version number when that version references it, else null. */
  published: number | null;
}

/**
 * Key → the agents whose draft or latest published spec references it, sorted by agent name, in one
 * pass. `keysOf` says what a spec references. Agents missing from `latestSpecs` are judged by their
 * draft alone.
 */
export function agentUsageByKey(
  agents: readonly Agent[],
  latestSpecs: ReadonlyMap<string, AgentSpec>,
  keysOf: (spec: AgentSpec | null | undefined) => Iterable<string>,
): Map<string, AgentUsage[]> {
  const byKey = new Map<string, AgentUsage[]>();
  for (const agent of agents) {
    const draft = new Set(keysOf(agent.draft_spec));
    const latest = agent.latest_version !== null ? new Set(keysOf(latestSpecs.get(agent.id))) : new Set<string>();
    for (const key of new Set([...draft, ...latest])) {
      const list = byKey.get(key) ?? [];
      list.push({ agentId: agent.id, agentName: agent.name, draft: draft.has(key), published: latest.has(key) ? agent.latest_version : null });
      byKey.set(key, list);
    }
  }
  for (const list of byKey.values()) list.sort((a, b) => a.agentName.localeCompare(b.agentName));
  return byKey;
}

/** "draft", "v3" or "draft, v3": where an agent uses something. */
export function describeUsage(usage: AgentUsage): string {
  const parts: string[] = [];
  if (usage.draft) parts.push("draft");
  if (usage.published !== null) parts.push(`v${usage.published}`);
  return parts.join(", ");
}

/** "3 agents", or "3+ agents" when some versions could not be read. */
export function usageCountLabel(count: number, partial: boolean): string {
  return `${count}${partial ? "+" : ""} agent${count === 1 && !partial ? "" : "s"}`;
}

// ---------------------------------------------------------------------------------------------
// The hook

export type AgentSpecsStatus = "loading" | "ready" | "error";

export interface AgentLatestSpecsState {
  status: AgentSpecsStatus;
  /** A background re-check (`recheck`) is running; the previous result stays meanwhile. */
  refreshing: boolean;
  /** Some agents' latest versions could not be read; their drafts were still checked. */
  partial: boolean;
  /** The agents list (empty until `status` is "ready"). */
  agents: Agent[];
  /** Per agent, in list order (empty until "ready"). */
  specs: AgentLatestSpec[];
  /** Agent id → readable latest spec. */
  latestSpecs: ReadonlyMap<string, AgentSpec>;
  /** Load again now. */
  reload: () => void;
  /** Load again only when the last result is older than AGENT_SPECS_FRESH_MS (or failed). */
  refreshIfStale: () => void;
  /** Always re-check, keeping the current result visible meanwhile (`refreshing`). For decisions
   * (a delete confirm) that must not act on a cached answer. */
  recheck: () => void;
}

const NO_AGENTS: Agent[] = [];
const NO_SPECS: AgentLatestSpec[] = [];

/**
 * Loads `GET /agents` and every published agent's latest version (`loadAgentLatestSpecs`). A failed
 * agents list gives status "error"; a failed version read only marks the result `partial`.
 */
export function useAgentLatestSpecs(): AgentLatestSpecsState {
  const [status, setStatus] = useState<AgentSpecsStatus>("loading");
  const [refreshing, setRefreshing] = useState(false);
  const [result, setResult] = useState<{ agents: Agent[]; specs: AgentLatestSpec[] }>({ agents: NO_AGENTS, specs: NO_SPECS });
  const generation = useRef(0);
  const loadedAt = useRef<number | null>(null);
  const statusRef = useRef<AgentSpecsStatus>("loading");
  statusRef.current = status;

  /** `background`: keep the current result and status, and flag `refreshing` instead. */
  const load = useCallback(async (background = false) => {
    const current = ++generation.current;
    if (background) setRefreshing(true);
    else setStatus("loading");
    try {
      const list = await withTimeout((signal) => api.agents.list({ signal }));
      const specs = await loadAgentLatestSpecs(list);
      if (current !== generation.current) return;
      setResult({ agents: list, specs });
      setStatus("ready");
      loadedAt.current = Date.now();
    } catch {
      if (current !== generation.current) return;
      setResult({ agents: NO_AGENTS, specs: NO_SPECS });
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

  const reload = useCallback(() => void load(), [load]);
  const refreshIfStale = useCallback(() => {
    if (statusRef.current === "loading") return;
    if (loadedAt.current !== null && Date.now() - loadedAt.current < AGENT_SPECS_FRESH_MS) return;
    void load();
  }, [load]);
  // A ready result stays on screen while the re-check runs; otherwise this is a plain load.
  const recheck = useCallback(() => void load(statusRef.current === "ready"), [load]);

  const latestSpecs = useMemo(() => latestSpecMap(result.specs), [result.specs]);
  const partial = useMemo(() => result.specs.some((item) => item.failed), [result.specs]);

  return useMemo(
    () => ({ status, refreshing, partial, agents: result.agents, specs: result.specs, latestSpecs, reload, refreshIfStale, recheck }),
    [status, refreshing, partial, result, latestSpecs, reload, refreshIfStale, recheck],
  );
}
