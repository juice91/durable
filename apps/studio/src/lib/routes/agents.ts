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

export interface RequestContextRouteOptions {
  agent?: string | null;
  version?: RouteVersion;
}

export const agentRoutes = {
  /** `/agents`: the agents list. */
  agents: (): string => "/agents",
  /** `/agents/new`: the create form. */
  agentNew: (): string => "/agents/new",
  /** `/agents/:id`: today's agent editor page. Named for the editor so SL.6, which turns `/agents/:id`
   * into the chat redirect, moves this builder to `/agents/:id/editor` without retargeting links. */
  agentEditor: (id: string): string => `/agents/${seg(id)}`,
  /** `/playground?agent=&conversation=&version=&model=&context=` (keys in that order). */
  playground: (opts: PlaygroundRouteOptions = {}): string =>
    withQuery("/playground", {
      agent: opts.agent,
      conversation: opts.conversation,
      version: opts.version === null || opts.version === undefined ? undefined : String(opts.version),
      model: opts.model,
      context: opts.context,
    }),
  /** `/request-context?agent=&version=`. */
  requestContext: (opts: RequestContextRouteOptions = {}): string =>
    withQuery("/request-context", {
      agent: opts.agent,
      version: opts.version === null || opts.version === undefined ? undefined : String(opts.version),
    }),
};
