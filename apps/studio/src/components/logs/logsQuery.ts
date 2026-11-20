// Studio logs (P0.20): the GET /logs client, the Logs page URL filters, the API query they become,
// error classification, and the "load older" cursor. The API builds the LogQL; Studio only sends
// structured parameters (run_id, agent_id, q, level, from, to, limit).
import { ApiError, BASE_URL, apiErrorFromResponse, apiHeaders } from "@/lib/api";
import { routes } from "@/lib/routes";
import { versionLabel } from "@/components/chat/runOptions";
import type { TraceRun } from "@/lib/traces";

export type LogLevel = "debug" | "info" | "warn" | "error" | "fatal";
export const LOG_LEVELS: readonly LogLevel[] = ["debug", "info", "warn", "error", "fatal"];

/** One record of GET /logs (newest first). `ts` is ISO 8601 UTC with up to microsecond precision. */
export interface LogItem {
  ts: string;
  level: string | null;
  message: string;
  service: string | null;
  trace_id: string | null;
  span_id: string | null;
  run_id: string | null;
}

export interface LogsResponse {
  items: LogItem[];
  /** The window the API searched (ISO). */
  from: string;
  to: string;
  limit: number;
  /** With `agent_id`: how many of the agent's runs were searched, and whether older runs in the
   * window were left out (the API covers at most its 200 newest runs there). */
  agent_runs?: number;
  agent_runs_limited?: boolean;
}

/** API bounds (apps/api routes/logs.py). */
export const MAX_LOG_LIMIT = 500;
export const MAX_QUERY_TEXT = 500;
export const MAX_RANGE_MS = 7 * 24 * 60 * 60 * 1000;
export const PAGE_LIMIT = 100;
/** Logs page search-box debounce before the URL (and so the query) changes. */
export const SEARCH_DEBOUNCE_MS = 300;
/** How many of an agent's most recent runs the Logs page run picker suggests (and the most the API's
 * agent filter searches in one window). */
export const AGENT_RUNS_LIMIT = 200;

export interface LogsQueryParams {
  run_id?: string | null;
  agent_id?: string | null;
  q?: string | null;
  level?: LogLevel | null;
  from?: string | null;
  to?: string | null;
  limit?: number;
}

export function logsQueryString(params: LogsQueryParams): string {
  const search = new URLSearchParams();
  if (params.run_id) search.set("run_id", params.run_id);
  if (params.agent_id) search.set("agent_id", params.agent_id);
  if (params.q) search.set("q", params.q);
  if (params.level) search.set("level", params.level);
  if (params.from) search.set("from", params.from);
  if (params.to) search.set("to", params.to);
  if (params.limit !== undefined) search.set("limit", String(params.limit));
  return search.toString();
}

export async function searchLogs(params: LogsQueryParams, signal?: AbortSignal): Promise<LogsResponse> {
  const query = logsQueryString(params);
  const response = await fetch(`${BASE_URL}/api/v1/logs${query ? `?${query}` : ""}`, {
    headers: apiHeaders(),
    cache: "no-store",
    signal,
  });
  if (!response.ok) throw await apiErrorFromResponse(response);
  return response.json();
}

// ---------------------------------------------------------------------------- errors

export type LogsErrorKind = "unavailable" | "not_found" | "agent_not_found" | "invalid" | "failed";

export interface LogsError {
  kind: LogsErrorKind;
  message: string;
}

/** What the UI shows for a failed search. 503 (Loki not configured, down, timing out, or all
 * query slots busy) is the "unavailable" state; the API's fixed message is shown as is. */
export function classifyLogsError(error: unknown): LogsError {
  if (error instanceof ApiError) {
    if (error.status === 503) return { kind: "unavailable", message: error.message || "Log search is unavailable" };
    // GET /logs answers 404 for an unknown run ("Run not found") or agent ("Agent not found").
    if (error.status === 404) {
      return /agent/i.test(error.message)
        ? { kind: "agent_not_found", message: "Agent not found" }
        : { kind: "not_found", message: "Run not found" };
    }
    if (error.status === 422) return { kind: "invalid", message: error.message || "Invalid log search" };
    if (error.status === 502) return { kind: "failed", message: "Log search failed" };
    if (error.status === 0) return { kind: "failed", message: error.message || "No API credentials configured" };
    return { kind: "failed", message: error.message || `Log search failed (HTTP ${error.status})` };
  }
  return { kind: "failed", message: "Could not reach the API" };
}

// ---------------------------------------------------------------------------- URL filters

export type RangePreset = "15m" | "1h" | "6h" | "24h" | "7d";
export const RANGE_PRESETS: ReadonlyArray<{ value: RangePreset; label: string; ms: number }> = [
  { value: "15m", label: "Last 15 minutes", ms: 15 * 60 * 1000 },
  { value: "1h", label: "Last hour", ms: 60 * 60 * 1000 },
  { value: "6h", label: "Last 6 hours", ms: 6 * 60 * 60 * 1000 },
  { value: "24h", label: "Last 24 hours", ms: 24 * 60 * 60 * 1000 },
  { value: "7d", label: "Last 7 days", ms: MAX_RANGE_MS },
];
/** How much shorter than the API maximum the widest preset's window is. */
export const PRESET_HEADROOM_MS = 60_000;
const PRESET_MS = new Map(RANGE_PRESETS.map((p) => [p.value, p.ms]));

/** The Logs page URL state: `q`, `level`, `agent_id`, `run_id`, and the range as `range=<preset>`
 * or `from`/`to` (ISO). No range means the API default (last hour, or the run's lifetime). */
export interface LogFilters {
  q: string | null;
  level: LogLevel | null;
  agent_id: string | null;
  run_id: string | null;
  range: RangePreset | null;
  from: string | null;
  to: string | null;
}

export const EMPTY_LOG_FILTERS: LogFilters = {
  q: null, level: null, agent_id: null, run_id: null, range: null, from: null, to: null,
};

type ParamsLike = { get(name: string): string | null };

export function parseLogFilters(params: ParamsLike): LogFilters {
  const one = (name: string) => {
    const v = params.get(name);
    return v !== null && v.trim() !== "" ? v.trim() : null;
  };
  const level = one("level");
  const range = one("range");
  const from = one("from");
  const to = one("to");
  const custom = from !== null || to !== null;
  return {
    // The search text keeps inner whitespace; only the URL's empty value means "no text".
    q: (() => {
      const v = params.get("q");
      return v !== null && v.trim() !== "" ? v.slice(0, MAX_QUERY_TEXT) : null;
    })(),
    level: level && (LOG_LEVELS as readonly string[]).includes(level) ? (level as LogLevel) : null,
    agent_id: one("agent_id"),
    run_id: one("run_id"),
    range: !custom && range && PRESET_MS.has(range as RangePreset) ? (range as RangePreset) : null,
    from,
    to,
  };
}

export function logFiltersToParams(filters: LogFilters): URLSearchParams {
  const params = new URLSearchParams();
  if (filters.q) params.set("q", filters.q);
  if (filters.level) params.set("level", filters.level);
  if (filters.agent_id) params.set("agent_id", filters.agent_id);
  if (filters.run_id) params.set("run_id", filters.run_id);
  if (filters.from || filters.to) {
    if (filters.from) params.set("from", filters.from);
    if (filters.to) params.set("to", filters.to);
  } else if (filters.range) {
    params.set("range", filters.range);
  }
  return params;
}

export function logsPath(filters: Partial<LogFilters>): string {
  return routes.logs(logFiltersToParams({ ...EMPTY_LOG_FILTERS, ...filters }));
}

const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
export function isUuid(value: string): boolean {
  return UUID_RE.test(value);
}

/** Why the filters cannot be searched (shown inline; no request is sent), or null. */
export function logFiltersProblem(filters: LogFilters): string | null {
  if (filters.run_id && !isUuid(filters.run_id)) return "Run id must be a UUID";
  if (filters.agent_id && !isUuid(filters.agent_id)) return "Unknown agent";
  const from = filters.from ? Date.parse(filters.from) : null;
  const to = filters.to ? Date.parse(filters.to) : null;
  if (from !== null && Number.isNaN(from)) return "'From' is not a valid time";
  if (to !== null && Number.isNaN(to)) return "'To' is not a valid time";
  if (from !== null && to !== null) {
    if (from >= to) return "'From' must be before 'To'";
    if (to - from > MAX_RANGE_MS) return "The time range may span at most 7 days";
  }
  return null;
}

/** The API query for the page's filters at time `now`. A preset becomes `from` = now - preset (the
 * API's `to` defaults to now). With a run and no range, `run` (when loaded) gives the run's
 * lifetime window. The agent filter is the API's `agent_id` (its runs in the window). */
export function logsRequestParams(
  filters: LogFilters,
  now: number = Date.now(),
  limit: number = PAGE_LIMIT,
  run: RunTimes | null = null,
): LogsQueryParams {
  const params: LogsQueryParams = { limit };
  if (filters.run_id) params.run_id = filters.run_id;
  if (filters.agent_id) params.agent_id = filters.agent_id;
  if (filters.q) params.q = filters.q;
  if (filters.level) params.level = filters.level;
  if (filters.from || filters.to) {
    if (filters.from) params.from = filters.from;
    if (filters.to) params.to = filters.to;
  } else if (filters.range) {
    // A window of exactly the API maximum is rejected (422) once `to` (now on the server) passes
    // `from + 7 days`: keep a minute of headroom.
    const ms = Math.min(PRESET_MS.get(filters.range) ?? 0, MAX_RANGE_MS - PRESET_HEADROOM_MS);
    params.from = new Date(now - ms).toISOString();
  } else if (filters.run_id && run) {
    const lifetime = runLogsWindow(run, now);
    if (lifetime) Object.assign(params, lifetime);
  }
  return params;
}

// ---------------------------------------------------------------------------- paging

/** Microseconds since the epoch of an ISO timestamp, keeping the API's sub-millisecond digits
 * (Date.parse drops them). Null when unparsable. */
export function isoToMicros(iso: string): number | null {
  const m = /^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.(\d+))?(Z|[+-]\d\d:?\d\d)?$/i.exec(iso.trim());
  if (!m) return null;
  const seconds = Date.parse(`${m[1]}${m[3] ?? "Z"}`);
  if (Number.isNaN(seconds)) return null;
  const fraction = Number((m[2] ?? "").padEnd(6, "0").slice(0, 6));
  return seconds * 1000 + fraction;
}

export function microsToIso(micros: number): string {
  const seconds = Math.floor(micros / 1_000_000);
  const rest = micros - seconds * 1_000_000;
  return new Date(seconds * 1000).toISOString().replace(/\.\d{3}Z$/, `.${String(rest).padStart(6, "0")}Z`);
}

/** The `to` for the next older page: one microsecond after the oldest item shown. Loki's end is
 * exclusive and the API's timestamps are cut to microseconds, so this re-includes records in that
 * same microsecond (deduplicated by `logKey`) instead of skipping them. */
export function olderPageTo(items: LogItem[]): string | null {
  let oldest: number | null = null;
  for (const item of items) {
    const t = isoToMicros(item.ts);
    if (t !== null && (oldest === null || t < oldest)) oldest = t;
  }
  return oldest === null ? null : microsToIso(oldest + 1);
}

export function logKey(item: LogItem): string {
  return [item.ts, item.service ?? "", item.span_id ?? "", item.run_id ?? "", item.level ?? "", item.message].join("\u0000");
}

/** One React key per row: `logKey`, plus the occurrence index for the second and later copies of
 * an identical record in the same list (the API can return exact duplicates). */
export function logRowKeys(items: readonly LogItem[]): string[] {
  const seen = new Map<string, number>();
  return items.map((item) => {
    const base = logKey(item);
    const count = seen.get(base) ?? 0;
    seen.set(base, count + 1);
    return count === 0 ? base : `${base}\u0000#${count}`;
  });
}

/** `older` appended after `current`, without records already shown. */
export function appendOlder(current: LogItem[], older: LogItem[]): { items: LogItem[]; added: number } {
  const seen = new Set(current.map(logKey));
  const fresh: LogItem[] = [];
  for (const item of older) {
    const key = logKey(item);
    if (seen.has(key)) continue;
    seen.add(key);
    fresh.push(item);
  }
  return { items: fresh.length ? [...current, ...fresh] : current, added: fresh.length };
}

// ---------------------------------------------------------------------------- trace panel

export interface SpanWindow {
  start: string | null;
  end: string | null;
}

/** The API query for the trace view's logs panel: the run's logs in the span's window when
 * `narrow` and the span has a start (the end is inclusive, Loki's is exclusive; an open span runs
 * to now), else in the run's lifetime (`runLogsWindow`) when the run is known. */
export function traceLogsParams(
  runId: string,
  window: SpanWindow | null,
  narrow: boolean,
  limit: number = MAX_LOG_LIMIT,
  run: RunTimes | null = null,
  now: number = Date.now(),
): LogsQueryParams {
  const params: LogsQueryParams = { run_id: runId, limit };
  const start = narrow && window?.start ? isoToMicros(window.start) : null;
  if (start === null) {
    const lifetime = run ? runLogsWindow(run, now) : null;
    return lifetime ? { ...params, ...lifetime } : params;
  }
  const end = window?.end ? isoToMicros(window.end) : null;
  if (end !== null && end <= start) {
    // A zero-length span (a processor's block, a refused tool call) is stamped at one instant, and
    // its own log lines land a few milliseconds either side of it: pad the window both ways so they
    // show (the browser check found a 0 ms span's blocking log line missing).
    params.from = microsToIso(start - ZERO_SPAN_PAD_MS * 1000);
    params.to = microsToIso(start + ZERO_SPAN_PAD_MS * 1000 + 1000);
    return params;
  }
  params.from = microsToIso(start);
  if (end !== null) params.to = microsToIso(end + 1000);
  return params;
}

/** How far a zero-length span's log window reaches each side of its instant. */
export const ZERO_SPAN_PAD_MS = 250;

// ---------------------------------------------------------------------------- run window

/** The run fields the run-lifetime window needs (GET /runs/{id} and the trace's run). */
export interface RunTimes {
  created_at: string;
  updated_at?: string | null;
  finished_at?: string | null;
  display_status?: string | null;
}

/** Like the API's run lookback: the window starts a minute before the run was created. */
export const RUN_LOOKBACK_MS = 60 * 1000;
/** The worker still logs after it records a run's finish, so the window runs this long past it. */
export const RUN_TRAILING_MS = 5 * 1000;
const TERMINAL_STATUSES = new Set(["completed", "skipped", "failed", "needs_attention"]);

/** Margin below the API's 7-day cap when an open-ended window's start is set from the browser's
 * clock (which may be ahead of the server's). */
const OPEN_WINDOW_MARGIN_MS = 60 * 60 * 1000;

/** The run's lifetime as a log window, so an old, short run never scans days of logs (the API's
 * default for a run id runs to now):
 * - a finished run: created_at - 1 minute → finished_at (or updated_at without it) + 5 s (the
 *   worker logs after recording the finish), at most 7 days, keeping the end;
 * - a run still going, or a finish + 5 s not yet past: no `to`, so the API ends the window at
 *   ITS now (no browser clock skew can drop recent records); `from` = created_at - 1 minute, unless
 *   the run started more than 7 days - 1 hour ago by the browser's clock, in which case `from` is
 *   left out too and the API's default for a run id applies: max(created_at - 1 minute,
 *   now - 7 days), which can never be a 422.
 * Null when created_at is unreadable. */
export function runLogsWindow(run: RunTimes, now: number = Date.now()): { from?: string; to?: string } | null {
  const created = isoToMicros(run.created_at);
  if (created === null) return null;
  const terminal = run.display_status ? TERMINAL_STATUSES.has(run.display_status) : !!run.finished_at;
  const finished = run.finished_at ? isoToMicros(run.finished_at) : null;
  const end = finished ?? (terminal && run.updated_at ? isoToMicros(run.updated_at) : null);
  const start = created - RUN_LOOKBACK_MS * 1000;
  if (end !== null) {
    const to = Math.max(end, created) + RUN_TRAILING_MS * 1000;
    if (to <= now * 1000) {
      return { from: microsToIso(Math.max(start, to - MAX_RANGE_MS * 1000)), to: microsToIso(to) };
    }
  }
  if (now * 1000 - start > (MAX_RANGE_MS - OPEN_WINDOW_MARGIN_MS) * 1000) return {};
  return { from: microsToIso(start) };
}

// ---------------------------------------------------------------------------- display

/** Local wall-clock time with milliseconds, and the date (for the title / second line). */
export function formatLogTime(iso: string): { time: string; date: string } {
  const t = Date.parse(iso);
  if (Number.isNaN(t)) return { time: iso, date: "" };
  const d = new Date(t);
  const pad = (n: number, w = 2) => String(n).padStart(w, "0");
  return {
    time: `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}.${pad(d.getMilliseconds(), 3)}`,
    date: `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`,
  };
}

// ---------------------------------------------------------------------------- run picker

/** A run suggestion in the Logs page's run picker: when, the agent version ("Draft" for a draft run) and status. */
export function runOptionLabel(run: TraceRun): string {
  const when = new Date(run.created_at).toLocaleString();
  // Agent runs name their version, or "Draft" for a draft test run or turn (follow-up 1c).
  const version = run.agent_id ? versionLabel(run.draft === true, run.version) : null;
  return [when, version, run.display_status].filter(Boolean).join(" · ");
}
