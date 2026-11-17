/**
 * Every internal Studio URL comes from here (decision "Routes module and redirects"): components
 * and tests call `routes.trace(id)` instead of writing "/traces/…", so a route move edits one
 * builder and the assertions that name it. One file per area; legacy redirects live in `legacy.ts`.
 */
import { agentRoutes } from "./agents";
import { architectRoutes } from "./architect";
import { approvalRoutes } from "./approvals";
import { evaluationRoutes } from "./evaluation";
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
  approvals: approvalRoutes,
  agents: agentRoutes,
  architect: architectRoutes,
  workflows: workflowRoutes,
  tools: toolRoutes,
  mcp: mcpRoutes,
  processors: processorRoutes,
  knowledge: knowledgeRoutes,
  evaluation: evaluationRoutes,
  observability: observabilityRoutes,
  settings: settingsRoutes,
} as const;

export const routes = {
  ...approvalRoutes,
  ...agentRoutes,
  ...architectRoutes,
  ...workflowRoutes,
  ...toolRoutes,
  ...mcpRoutes,
  ...processorRoutes,
  ...knowledgeRoutes,
  ...evaluationRoutes,
  ...observabilityRoutes,
  ...settingsRoutes,
} as const;

export type Routes = typeof routes;

export { RouteIdError, seg, withQuery, type RouteQuery } from "./query";
export type {
  AgentChatRouteOptions,
  AgentEditorRouteOptions,
  AgentMemoryRouteOptions,
  AgentTab,
  PlaygroundRouteOptions,
  RequestContextRouteOptions,
  RouteVersion,
} from "./agents";
export { agentChatPathState, agentTabOf } from "./agents";
export { APPROVAL_SHEET_PARAM, type ApprovalsRouteOptions } from "./approvals";
export type { KnowledgeBaseRouteOptions, KnowledgeTab } from "./knowledge";
export { isSafeTarget, legacyTarget, LEGACY_RULES, type LegacyRule, type LegacySearchParams } from "./legacy";
