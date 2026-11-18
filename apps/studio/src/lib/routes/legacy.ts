/**
 * Legacy URL redirects (decision "Routes module and redirects"). `legacyTarget` maps an old Studio
 * URL to where it lives now, or returns null when the URL is current. Each old page that moves
 * becomes a small server component that awaits `searchParams` and calls
 * `redirect(legacyTarget(pathname, searchParams))`; no middleware.
 *
 * Rows are added per the plan's route map, one row group per task in this order: SL.6
 * (`/agents/:id`, `/playground`), SL.7 (`/`), SL.10 (`/request-context`), SL.12 (`/traces/:runId`,
 * `/runs/:id`).
 */

import { agentRoutes, type RouteVersion } from "./agents";
import { RouteIdError } from "./query";
import { observabilityRoutes, TRACE_LEGACY_RULES } from "./observability";
import { settingsRoutes } from "./settings";

export type LegacySearchParams =
  | URLSearchParams
  | Record<string, string | string[] | undefined>;

export interface LegacyRule {
  /** The task that retired the URL, e.g. "SL.6". */
  task: string;
  /** Old path pattern; `:name` matches one segment (decoded into `params`). */
  pattern: string;
  /** New URL for a match, or null when this rule does not apply to these parameters. */
  to: (params: Record<string, string>, search: URLSearchParams) => string | null;
}

/** The retired dashboard's URL parameters: its date range only (it ignored `interval` and the filters). */
const DASHBOARD_PARAMS: ReadonlySet<string> = new Set(["range", "from", "to"]);

/** A legacy `version` value as the agent chat takes it: "draft" or a positive integer; anything else is dropped. */
function legacyVersion(raw: string | null): RouteVersion {
  if (raw === "draft") return "draft";
  return raw && /^[1-9]\d{0,8}$/.test(raw) ? Number(raw) : null;
}

/** The agent's chat, or the agents list when an id cannot be a path segment ("." or ".."). */
function chatOrAgents(agent: string, opts: Parameters<typeof agentRoutes.agentChat>[1] = {}): string {
  try {
    return agentRoutes.agentChat(agent, opts);
  } catch (error) {
    if (error instanceof RouteIdError) return agentRoutes.agents();
    throw error;
  }
}

/** Rule rows, first match wins. */
export const LEGACY_RULES: readonly LegacyRule[] = [
  // SL.6: the agent page. `/agents/:id` was the editor; it now opens the agent's chat
  // (`/agents/new` is the create form, never a legacy URL).
  { task: "SL.6", pattern: "/agents/:id", to: ({ id }) => (id === "new" ? null : chatOrAgents(id)) },
  // SL.6: the playground moved into the agent page's Chat tab, keeping its options. Without an agent
  // there is nothing to route to on the server: the page's client redirect picks the last-used agent.
  {
    task: "SL.6",
    pattern: "/playground",
    to: (_params, search) => {
      const agent = search.get("agent");
      if (!agent) return null;
      return chatOrAgents(agent, {
        conversation: search.get("conversation"),
        version: legacyVersion(search.get("version")),
        model: search.get("model"),
        context: search.get("context"),
      });
    },
  },
  // SL.7: the dashboard folded into Metrics' Overview. With a dashboard parameter, `/` opens Metrics
  // on the same range (only the dashboard's own parameters are carried, in their order); a bare `/`
  // (or one with unrelated parameters) opens Agents.
  {
    task: "SL.7",
    pattern: "/",
    to: (_params, search) => {
      const carried = new URLSearchParams();
      for (const [key, value] of search) if (DASHBOARD_PARAMS.has(key)) carried.append(key, value);
      return carried.toString() ? observabilityRoutes.metrics(carried) : agentRoutes.agents();
    },
  },
  // SL.10: the Request Context page became the chat composer's popover (with named presets). With an
  // agent it opens that agent's chat on the same version with the popover open; without one, Agents.
  {
    task: "SL.10",
    pattern: "/request-context",
    to: (_params, search) => {
      const agent = search.get("agent");
      if (!agent) return agentRoutes.agents();
      return chatOrAgents(agent, { version: legacyVersion(search.get("version")), context: "open" });
    },
  },
  // Settings tabs: the Providers page became the Providers tab of Settings.
  { task: "ST.1", pattern: "/settings/providers", to: () => settingsRoutes.settingsProviders() },
  // SL.12: the run and trace pages became the trace Sheet on Traces (rules defined with the routes).
  ...TRACE_LEGACY_RULES,
];

/** Next 15 `searchParams` (a record, values possibly arrays) or URLSearchParams, as URLSearchParams. */
export function toSearchParams(input?: LegacySearchParams | null): URLSearchParams {
  if (!input) return new URLSearchParams();
  if (input instanceof URLSearchParams) return new URLSearchParams(input);
  const out = new URLSearchParams();
  for (const [key, value] of Object.entries(input)) {
    if (value === undefined) continue;
    for (const v of Array.isArray(value) ? value : [value]) out.append(key, v);
  }
  return out;
}

/** Match `pathname` against `pattern`; the decoded `:name` segments, or null. A trailing slash is ignored. */
export function matchPattern(pattern: string, pathname: string): Record<string, string> | null {
  const trim = (p: string) => (p.length > 1 && p.endsWith("/") ? p.slice(0, -1) : p);
  const want = trim(pattern).split("/");
  const got = trim(pathname).split("/");
  if (want.length !== got.length) return null;
  const params: Record<string, string> = {};
  for (let i = 0; i < want.length; i++) {
    const w = want[i];
    const g = got[i];
    if (w.startsWith(":")) {
      if (!g) return null;
      try {
        params[w.slice(1)] = decodeURIComponent(g);
      } catch {
        return null;
      }
    } else if (w !== g) {
      return null;
    }
  }
  return params;
}

/** Where an old URL lives now, or null when it is not a legacy URL. */
export function legacyTarget(
  pathname: string,
  searchParams?: LegacySearchParams | null,
  rules: readonly LegacyRule[] = LEGACY_RULES,
): string | null {
  const search = toSearchParams(searchParams);
  for (const rule of rules) {
    const params = matchPattern(rule.pattern, pathname);
    if (!params) continue;
    let target: string | null;
    try {
      target = rule.to(params, search);
    } catch (error) {
      // An id no path can carry ("." or ".."): no target from this rule.
      if (error instanceof RouteIdError) continue;
      throw error;
    }
    if (target === null) continue;
    if (isSafeTarget(target)) return target;
    if (process.env.NODE_ENV !== "production") {
      console.warn(`legacyTarget: rule ${rule.task} ${rule.pattern} produced an unsafe target, ignored: ${target}`);
    }
    return null;
  }
  return null;
}

const ORIGIN = "http://studio.invalid";

/**
 * A redirect target must stay on this origin: a path starting with one "/" (not "//" or "/\\"), with
 * no scheme, and still same-origin after a URL parser strips tabs and newlines and folds backslashes.
 */
export function isSafeTarget(target: string): boolean {
  if (!target.startsWith("/") || target.startsWith("//") || target.startsWith("/\\")) return false;
  if (/[\u0000-\u001f\u007f]/.test(target)) return false;
  try {
    return new URL(target, ORIGIN).origin === ORIGIN;
  } catch {
    return false;
  }
}
