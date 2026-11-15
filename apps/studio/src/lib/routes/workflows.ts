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

/**
 * The graph's collapsed loops (LP.6): `?collapsed=each,inner`, the loop step ids drawn collapsed,
 * sorted and comma-separated. Shared by the Graph tab and the run view, so a link between them keeps
 * the same view.
 */
export const WORKFLOW_COLLAPSED_PARAM = "collapsed";
const STEP_ID = /^[a-z][a-z0-9_]{0,63}$/;
const MAX_COLLAPSED = 50;

/** The loop ids in `?collapsed=`; anything that is not a step id is dropped. */
export function parseCollapsedParam(raw: string | null | undefined): Set<string> {
  if (!raw) return new Set();
  return new Set(raw.split(",").map((part) => part.trim()).filter((part) => STEP_ID.test(part)).slice(0, MAX_COLLAPSED));
}

/** The `?collapsed=` value for `ids` (sorted, so one set has one URL), or null when empty. */
export function collapsedParam(ids: ReadonlySet<string> | readonly string[] | null | undefined): string | null {
  if (!ids) return null;
  const list = [...ids].filter((id) => STEP_ID.test(id)).sort();
  return list.length > 0 ? list.join(",") : null;
}

export interface WorkflowRouteOptions {
  /** `graph` (default, left out) or `runs`. The Traces tab is `workflowTraces`. */
  tab?: Exclude<WorkflowTab, "traces"> | null;
  /** Loops drawn collapsed on the Graph tab (`?collapsed=`); left out when empty. */
  collapsed?: ReadonlySet<string> | null;
}

export interface WorkflowRunRouteOptions {
  /** Loops drawn collapsed in the run view's graph (`?collapsed=`); left out when empty. */
  collapsed?: ReadonlySet<string> | null;
}

function query(entries: Array<[string, string | null]>): string {
  const params = new URLSearchParams();
  for (const [key, value] of entries) if (value !== null) params.set(key, value);
  const text = params.toString();
  return text ? `?${text}` : "";
}

export const workflowRoutes = {
  /** `/workflows`: the workflows list. */
  workflows: (): string => "/workflows",
  /** `/workflows/:id[?tab=runs]`: the workflow detail page (Graph, or the Runs tab). */
  workflow: (id: string, { tab = null, collapsed = null }: WorkflowRouteOptions = {}): string =>
    `/workflows/${seg(id)}${query([
      [WORKFLOW_TAB_PARAM, tab === "runs" ? "runs" : null],
      [WORKFLOW_COLLAPSED_PARAM, tab === "runs" ? null : collapsedParam(collapsed)],
    ])}`,
  /** `/workflows/:id/traces`: the workflow's Traces tab (its runs, opening the trace Sheet). */
  workflowTraces: (id: string): string => `/workflows/${seg(id)}/traces`,
  /** `/workflows/:id/runs/:runId`: one workflow run. */
  workflowRun: (id: string, runId: string, { collapsed = null }: WorkflowRunRouteOptions = {}): string =>
    `/workflows/${seg(id)}/runs/${seg(runId)}${query([[WORKFLOW_COLLAPSED_PARAM, collapsedParam(collapsed)]])}`,
};
