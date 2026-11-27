import { seg, withQuery } from "./query";

/** The workflow detail page's tabs (SL.14). Graph is the default and is left out of URLs. */
export const WORKFLOW_TABS = ["graph", "runs", "traces", "access"] as const;
export type WorkflowTab = (typeof WORKFLOW_TABS)[number];
/** The detail page's own tab parameter (`/workflows/:id?tab=runs`). */
export const WORKFLOW_TAB_PARAM = "tab";
/**
 * The trace Sheet's tab parameter on workflow pages: `tab` is the page's own (`?tab=runs`), so the
 * Sheet hosted there (`TraceSheetHost tabParam`) uses this one.
 */
export const WORKFLOW_TRACE_TAB_PARAM = "traceTab";

/** The `?tab=` value of `/workflows/:id`: `runs` or `access`, else `graph`. (Traces is its own path.) */
export function parseWorkflowTab(raw: string | null | undefined): Exclude<WorkflowTab, "traces"> {
  return raw === "runs" || raw === "access" ? raw : "graph";
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

/**
 * The run view's iteration pagers (LP.12): `?iter=each.41,outer.1/inner.2`, one iteration scope
 * string per loop instance whose pager the user moved (the last frame names the loop and the index;
 * the prefix is the enclosing iteration). Sorted, so one choice has one URL. Pagers not listed
 * follow their latest iteration.
 */
export const WORKFLOW_ITERATION_PARAM = "iter";
/** Loop instances (`each`, `outer.1/inner`) whose pager steps through failed iterations only. */
export const WORKFLOW_FAILED_ONLY_PARAM = "failedOnly";
const FRAME = "[a-z][a-z0-9_]{0,63}\\.(?:0|[1-9][0-9]{0,5})";
const ITERATION_SCOPE = new RegExp(`^(?:${FRAME}/){0,3}${FRAME}$`);
const LOOP_INSTANCE = new RegExp(`^(?:${FRAME}/){0,3}[a-z][a-z0-9_]{0,63}$`);
const MAX_ITERATION_ENTRIES = 50;

function listParam(raw: string | null | undefined, pattern: RegExp): string[] {
  if (!raw) return [];
  return raw.split(",").map((part) => part.trim()).filter((part) => pattern.test(part)).slice(0, MAX_ITERATION_ENTRIES);
}

/** `?iter=` as {loop instance key: index}; malformed entries are dropped. */
export function parseIterationParam(raw: string | null | undefined): Record<string, number> {
  const out: Record<string, number> = {};
  for (const scope of listParam(raw, ITERATION_SCOPE)) {
    const dot = scope.lastIndexOf(".");
    out[scope.slice(0, dot)] = Number(scope.slice(dot + 1));
  }
  return out;
}

/** The `?iter=` value for a selection (sorted), or null when empty. */
export function iterationParam(selection: Readonly<Record<string, number>> | null | undefined): string | null {
  if (!selection) return null;
  const list = Object.entries(selection)
    .filter(([key, index]) => Number.isInteger(index) && index >= 0 && ITERATION_SCOPE.test(`${key}.${index}`))
    .map(([key, index]) => `${key}.${index}`)
    .sort();
  return list.length > 0 ? list.join(",") : null;
}

/** `?failedOnly=` as a set of loop instance keys. */
export function parseFailedOnlyParam(raw: string | null | undefined): Set<string> {
  return new Set(listParam(raw, LOOP_INSTANCE));
}

/** The `?failedOnly=` value (sorted), or null when empty. */
export function failedOnlyParam(keys: ReadonlySet<string> | null | undefined): string | null {
  if (!keys) return null;
  const list = [...keys].filter((key) => LOOP_INSTANCE.test(key)).sort();
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
  /** `/workflows[?view=archived]`: the workflows list (the archived workflows with `archived`). */
  workflows: (opts: { archived?: boolean } = {}): string => withQuery("/workflows", { view: opts.archived ? "archived" : undefined }),
  /** `/workflows/:id[?tab=runs]`: the workflow detail page (Graph, or the Runs tab). */
  workflow: (id: string, { tab = null, collapsed = null }: WorkflowRouteOptions = {}): string =>
    `/workflows/${seg(id)}${query([
      [WORKFLOW_TAB_PARAM, tab === "runs" || tab === "access" ? tab : null],
      [WORKFLOW_COLLAPSED_PARAM, tab === "runs" || tab === "access" ? null : collapsedParam(collapsed)],
    ])}`,
  /** `/workflows/schedules`: every workflow schedule. */
  workflowSchedules: (): string => "/workflows/schedules",
  /** `/workflows/:id/schedules`: one workflow's Schedules tab. */
  workflowSchedule: (id: string): string => `/workflows/${seg(id)}/schedules`,
  /** `/workflows/:id/traces`: the workflow's Traces tab (its runs, opening the trace Sheet). */
  workflowTraces: (id: string): string => `/workflows/${seg(id)}/traces`,
  /** `/workflows/:id/runs/:runId`: one workflow run. */
  workflowRun: (id: string, runId: string, { collapsed = null }: WorkflowRunRouteOptions = {}): string =>
    `/workflows/${seg(id)}/runs/${seg(runId)}${query([[WORKFLOW_COLLAPSED_PARAM, collapsedParam(collapsed)]])}`,
};
