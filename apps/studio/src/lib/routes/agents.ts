import { seg, withQuery } from "./query";

/** Version selector in Studio URLs: a published version number or "draft"; null or undefined means
 * the latest (the parameter is left out). */
export type RouteVersion = number | "draft" | null | undefined;

export interface PlaygroundRouteOptions {
  agent?: string | null;
  conversation?: string | null;
  version?: RouteVersion;
  model?: string | null;
  /** `context=preset`: the saved preset wins over the context the tab remembered (P0.19). */
  context?: string | null;
}

export interface AgentChatRouteOptions {
  /** Opens this conversation (`/agents/:id/chat/:conversation`); none starts a new one. */
  conversation?: string | null;
  version?: RouteVersion;
  model?: string | null;
  /** `context=preset` (the saved preset wins) or, from SL.10, `context=open`. */
  context?: string | null;
}

export interface AgentEditorRouteOptions {
  /** The stepped editor's step id (SL.11: `identity`, `instructions`, `tools`, `request-context`, `processors`). */
  step?: string | null;
  /** A version compare, `a..b` (SL.11): build it with `compareParam`. */
  compare?: string | null;
}

/** One side of a version compare in a URL: a published version number or "draft". */
export type CompareRouteRef = number | "draft";

/** The `?compare=` value for `base` against `target`: `3..draft`, `1..3`. */
export function compareParam(base: CompareRouteRef, target: CompareRouteRef): string {
  return `${base}..${target}`;
}

function parseCompareRef(value: string): CompareRouteRef | null {
  if (value === "draft") return "draft";
  if (!/^[1-9]\d{0,8}$/.test(value)) return null;
  return Number(value);
}

/** Parses `?compare=a..b` (each side a version number or "draft", the two different); null when malformed. */
export function parseCompareParam(value: string | null | undefined): { base: CompareRouteRef; target: CompareRouteRef } | null {
  const match = /^([^.]+)\.\.([^.]+)$/.exec(value ?? "");
  if (!match) return null;
  const base = parseCompareRef(match[1]);
  const target = parseCompareRef(match[2]);
  if (base === null || target === null || base === target) return null;
  return { base, target };
}

export interface AgentMemoryRouteOptions {
  /** The user (resource id) whose memory the tab shows (RM.18). */
  resource?: string | null;
}

export interface AgentTestsRouteOptions {
  /** The suite shown (AC.16); none lists the agent's suites. */
  suite?: string | null;
  /** An eval run of the suite: its results. */
  run?: string | null;
  /** Two eval runs compared, `base..target` (build with `runCompareParam` in lib/evals). */
  compare?: string | null;
}

export interface RequestContextRouteOptions {
  agent?: string | null;
  version?: RouteVersion;
}

function versionParam(version: RouteVersion): string | undefined {
  return version === null || version === undefined ? undefined : String(version);
}

/** Paths of the agent page's tabs, in header order. `memory` is listed only for an agent with memory
 * (RM.18); `tests` (AC.16) always; `access` only when the policy engine keeps grants (IAM P2c). */
export type AgentTab = "chat" | "traces" | "editor" | "memory" | "tests" | "access";

export const agentRoutes = {
  /** `/agents[?view=archived]`: the agents list (the archived agents with `archived`). */
  agents: (opts: { archived?: boolean } = {}): string => withQuery("/agents", { view: opts.archived ? "archived" : undefined }),
  /** `/agents/new`: the create form. */
  agentNew: (): string => "/agents/new",
  /** `/agents/:id`: the agent page's root, which redirects to the Chat tab (it was the editor before SL.6). */
  agentIndex: (id: string): string => `/agents/${seg(id)}`,
  /** `/agents/:id/chat[/:conversation]?version=&model=&context=` (keys in that order): the agent page's Chat tab. */
  agentChat: (id: string, opts: AgentChatRouteOptions = {}): string =>
    withQuery(opts.conversation ? `/agents/${seg(id)}/chat/${seg(opts.conversation)}` : `/agents/${seg(id)}/chat`, {
      version: versionParam(opts.version),
      model: opts.model,
      context: opts.context,
    }),
  /** `/agents/:id/traces`: the agent's runs. */
  agentTraces: (id: string): string => `/agents/${seg(id)}/traces`,
  /** `/agents/:id/editor?step=&compare=`: the editor, versions and compare. */
  agentEditor: (id: string, opts: AgentEditorRouteOptions = {}): string =>
    withQuery(`/agents/${seg(id)}/editor`, { step: opts.step, compare: opts.compare }),
  /** `/agents/:id/memory?resource=`: the agent's memory per user (RM.18; an agent without memory goes to Chat). */
  agentMemory: (id: string, opts: AgentMemoryRouteOptions = {}): string =>
    withQuery(`/agents/${seg(id)}/memory`, { resource: opts.resource }),
  /** `/agents/:id/tests?suite=&run=&compare=` (keys in that order): the agent's test suites (AC.16). */
  agentTests: (id: string, opts: AgentTestsRouteOptions = {}): string =>
    withQuery(`/agents/${seg(id)}/tests`, { suite: opts.suite, run: opts.run, compare: opts.compare }),
  /** `/agents/:id/access`: who holds which relation on the agent, and what it may use. */
  agentAccess: (id: string): string => `/agents/${seg(id)}/access`,
  /** The same tab of an agent (the header's agent switcher keeps the tab). */
  agentTab: (id: string, tab: AgentTab): string =>
    tab === "chat"
      ? agentRoutes.agentChat(id)
      : tab === "traces"
        ? agentRoutes.agentTraces(id)
        : tab === "memory"
          ? agentRoutes.agentMemory(id)
          : tab === "tests"
            ? agentRoutes.agentTests(id)
            : tab === "access"
              ? agentRoutes.agentAccess(id)
              : agentRoutes.agentEditor(id),
  /** `/playground?agent=&conversation=&version=&model=&context=` (keys in that order). A legacy URL since
   * SL.6: it redirects to `agentChat` (see `legacy.ts`). Kept for links that SL.10 retires. */
  playground: (opts: PlaygroundRouteOptions = {}): string =>
    withQuery("/playground", {
      agent: opts.agent,
      conversation: opts.conversation,
      version: versionParam(opts.version),
      model: opts.model,
      context: opts.context,
    }),
  /** `/request-context?agent=&version=`. */
  requestContext: (opts: RequestContextRouteOptions = {}): string =>
    withQuery("/request-context", {
      agent: opts.agent,
      version: versionParam(opts.version),
    }),
};

/** Which agent tab `pathname` shows (`/agents/a/chat/c` → "chat"), or null off the agent page. */
export function agentTabOf(pathname: string | null | undefined): AgentTab | null {
  const match = /^\/agents\/[^/]+\/(chat|traces|editor|memory|tests|access)(?:\/|$)/.exec(pathname ?? "");
  return match ? (match[1] as AgentTab) : null;
}

/** The agent and conversation a chat path names (`/agents/a/chat/c` → a, c), decoded; null when the
 * path is not an agent chat. */
export function agentChatPathState(pathname: string | null | undefined): { agentId: string; conversation: string | null } | null {
  const match = /^\/agents\/([^/]+)\/chat(?:\/([^/]+))?\/?$/.exec(pathname ?? "");
  if (!match) return null;
  try {
    return { agentId: decodeURIComponent(match[1]), conversation: match[2] ? decodeURIComponent(match[2]) : null };
  } catch {
    return null;
  }
}
