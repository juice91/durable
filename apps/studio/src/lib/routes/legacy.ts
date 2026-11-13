/**
 * Legacy URL redirects (decision "Routes module and redirects"). `legacyTarget` maps an old Studio
 * URL to where it lives now, or returns null when the URL is current. Each old page that moves
 * becomes a small server component that awaits `searchParams` and calls
 * `redirect(legacyTarget(pathname, searchParams))`; no middleware.
 *
 * The table is empty until a task retires a URL. Rows are added per the plan's route map, one row
 * group per task in this order: SL.6 (`/agents/:id`, `/playground`), SL.7 (`/`), SL.10
 * (`/request-context`), SL.12 (`/traces/:runId`, `/runs/:id`).
 */

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

/** Rule rows, first match wins. Empty in SL.3: no URL is retired yet. */
export const LEGACY_RULES: readonly LegacyRule[] = [];

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
    const target = rule.to(params, search);
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
