/**
 * Every internal Studio URL comes from here (decision "Routes module and redirects"): components
 * and tests call `routes.trace(id)` instead of writing "/traces/…", so a route move edits one
 * builder and the assertions that name it. One file per area; legacy redirects live in `legacy.ts`.
 */
import { agentRoutes } from "./agents";
import { knowledgeRoutes } from "./knowledge";
import { mcpRoutes } from "./mcp";
import { observabilityRoutes } from "./observability";
import { processorRoutes } from "./processors";
import { settingsRoutes } from "./settings";
import { toolRoutes } from "./tools";
import { workflowRoutes } from "./workflows";

/** Each area's builders. Builder names must be unique across areas: a later spread would silently
 * replace an earlier builder of the same name (checked in routes.test.ts). */
export const ROUTE_AREAS = {
  agents: agentRoutes,
  workflows: workflowRoutes,
  tools: toolRoutes,
  mcp: mcpRoutes,
  processors: processorRoutes,
  knowledge: knowledgeRoutes,
  observability: observabilityRoutes,
  settings: settingsRoutes,
} as const;

export const routes = {
  ...agentRoutes,
  ...workflowRoutes,
  ...toolRoutes,
  ...mcpRoutes,
  ...processorRoutes,
  ...knowledgeRoutes,
  ...observabilityRoutes,
  ...settingsRoutes,
} as const;

export type Routes = typeof routes;

export { RouteIdError, seg, withQuery, type RouteQuery } from "./query";
export type {
  AgentChatRouteOptions,
  AgentEditorRouteOptions,
  AgentTab,
  PlaygroundRouteOptions,
  RequestContextRouteOptions,
  RouteVersion,
} from "./agents";
export { agentChatPathState, agentTabOf } from "./agents";
export type { KnowledgeBaseRouteOptions, KnowledgeTab } from "./knowledge";
export { isSafeTarget, legacyTarget, LEGACY_RULES, type LegacyRule, type LegacySearchParams } from "./legacy";
