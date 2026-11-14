import { seg } from "./query";

/** The workflow detail page's tabs (SL.14). Graph is the default and is left out of URLs. */
export const WORKFLOW_TABS = ["graph", "runs", "traces"] as const;
export type WorkflowTab = (typeof WORKFLOW_TABS)[number];
/** The detail page's own tab parameter (`/workflows/:id?tab=runs`). */
export const WORKFLOW_TAB_PARAM = "tab";
/**
 * The trace Sheet's tab parameter on workflow pages: `tab` is the page's own (`?tab=runs`), so the
 * Sheet hosted there (`TraceSheetHost tabParam`) uses this one.
 */
export const WORKFLOW_TRACE_TAB_PARAM = "traceTab";

/** The `?tab=` value of `/workflows/:id`: `runs`, else `graph`. (Traces is its own path.) */
export function parseWorkflowTab(raw: string | null | undefined): Exclude<WorkflowTab, "traces"> {
  return raw === "runs" ? "runs" : "graph";
}

export interface WorkflowRouteOptions {
  /** `graph` (default, left out) or `runs`. The Traces tab is `workflowTraces`. */
  tab?: Exclude<WorkflowTab, "traces"> | null;
}

export const workflowRoutes = {
  /** `/workflows`: the workflows list. */
  workflows: (): string => "/workflows",
  /** `/workflows/:id[?tab=runs]`: the workflow detail page (Graph, or the Runs tab). */
  workflow: (id: string, { tab = null }: WorkflowRouteOptions = {}): string =>
    `/workflows/${seg(id)}${tab === "runs" ? `?${WORKFLOW_TAB_PARAM}=runs` : ""}`,
  /** `/workflows/:id/traces`: the workflow's Traces tab (its runs, opening the trace Sheet). */
  workflowTraces: (id: string): string => `/workflows/${seg(id)}/traces`,
  /** `/workflows/:id/runs/:runId`: one workflow run. */
  workflowRun: (id: string, runId: string): string => `/workflows/${seg(id)}/runs/${seg(runId)}`,
};
