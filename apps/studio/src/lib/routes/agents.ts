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
  /** The stepped editor's step (SL.11). */
  step?: string | null;
  /** A version compare, `a..b` (SL.11). */
  compare?: string | null;
}

export interface RequestContextRouteOptions {
  agent?: string | null;
  version?: RouteVersion;
}

function versionParam(version: RouteVersion): string | undefined {
  return version === null || version === undefined ? undefined : String(version);
}

/** Paths of the agent page's tabs, in header order. */
export type AgentTab = "chat" | "traces" | "editor";

export const agentRoutes = {
  /** `/agents`: the agents list. */
  agents: (): string => "/agents",
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
  /** The same tab of an agent (the header's agent switcher keeps the tab). */
  agentTab: (id: string, tab: AgentTab): string =>
    tab === "chat" ? agentRoutes.agentChat(id) : tab === "traces" ? agentRoutes.agentTraces(id) : agentRoutes.agentEditor(id),
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
  const match = /^\/agents\/[^/]+\/(chat|traces|editor)(?:\/|$)/.exec(pathname ?? "");
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
