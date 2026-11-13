"use client";

import React, { Suspense, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { api } from "@/lib/api";
import { getRun } from "@/lib/runs";
import { isoToLocalInput, localInputToIso } from "@/lib/datetimeLocal";
import { EMPTY_FILTERS, listTraceRuns, type TraceRun } from "@/lib/traces";
import { LogsTable } from "@/components/logs/LogsTable";
import { LogsUnavailable } from "@/components/logs/LogsUnavailable";
import { useLogSearch } from "@/components/logs/useLogSearch";
import {
  AGENT_RUNS_LIMIT,
  EMPTY_LOG_FILTERS,
  LOG_LEVELS,
  SEARCH_DEBOUNCE_MS,
  MAX_QUERY_TEXT,
  RANGE_PRESETS,
  agentRunFilter,
  isUuid,
  logFiltersProblem,
  logFiltersToParams,
  logsRequestParams,
  parseLogFilters,
  type LogFilters,
  type LogLevel,
  type RangePreset,
  type RunTimes,
} from "@/components/logs/logsQuery";

const inputClass =
  "rounded-md border border-input bg-background px-3 py-2 text-sm ring-offset-background placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2";
const buttonClass =
  "inline-flex items-center justify-center rounded-md bg-muted px-3 py-2 text-sm font-medium text-card-foreground hover:bg-muted/80 disabled:cursor-not-allowed disabled:opacity-50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring";

const LEVEL_LABELS: Record<LogLevel, string> = {
  debug: "Debug and above",
  info: "Info and above",
  warn: "Warn and above",
  error: "Error and above",
  fatal: "Fatal only",
};

function runOptionLabel(run: TraceRun): string {
  const when = new Date(run.created_at).toLocaleString();
  return `${when} · ${run.display_status}`;
}

interface RunOptions {
  runs: TraceRun[];
  loading: boolean;
  failed: boolean;
  /** The agent's run ids once loaded (null without an agent, while loading, or after a failure). */
  agentRunIds: Set<string> | null;
}

/** The agent's most recent runs (for the agent filter), or recent runs of any kind (for the run
 * picker's suggestions). Re-fetched on Refresh (`nonce`); a response for a previous agent is dropped. */
function useRunOptions(agentId: string | null, nonce: number): RunOptions {
  const [state, setState] = useState<{ agentId: string | null; runs: TraceRun[]; loading: boolean; failed: boolean }>({
    agentId, runs: [], loading: true, failed: false,
  });
  useEffect(() => {
    let cancelled = false;
    // A refresh for the same agent keeps the known runs until the new list arrives.
    setState((s) => (s.agentId === agentId ? { ...s, loading: true, failed: false } : { agentId, runs: [], loading: true, failed: false }));
    const filters = { ...EMPTY_FILTERS, agent_id: agentId };
    void Promise.resolve()
      .then(() => listTraceRuns(filters, { limit: agentId ? AGENT_RUNS_LIMIT : 50 }))
      .then(
        (page) => {
          if (!cancelled) setState({ agentId, runs: Array.isArray(page?.items) ? page.items : [], loading: false, failed: false });
        },
        () => {
          if (!cancelled) setState({ agentId, runs: [], loading: false, failed: true });
        },
      );
    return () => {
      cancelled = true;
    };
  }, [agentId, nonce]);
  const current = state.agentId === agentId;
  const known = current && !state.failed && (!state.loading || state.runs.length > 0);
  const agentRunIds = useMemo(
    () => (agentId && known ? new Set(state.runs.map((r) => r.run_id)) : null),
    [agentId, known, state.runs],
  );
  return { runs: current ? state.runs : [], loading: !current || state.loading, failed: current && state.failed, agentRunIds };
}

function formatWindowTime(iso: string): string {
  const date = new Date(iso);
  return Number.isNaN(date.getTime()) ? iso : date.toLocaleString();
}

/** The selected run's times, for the "Run lifetime" window. `status` "failed" means the window is
 * left to the API (which answers 404 for a run that is not the tenant's). Re-fetched on Refresh,
 * keeping the known run meanwhile so the query is not held back twice. */
function useSelectedRun(runId: string | null, nonce: number): { status: "none" | "loading" | "ok" | "failed"; run: RunTimes | null } {
  const [state, setState] = useState<{ runId: string | null; status: "loading" | "ok" | "failed"; run: RunTimes | null }>({
    runId: null, status: "loading", run: null,
  });
  useEffect(() => {
    if (!runId) return;
    let cancelled = false;
    setState((s) => (s.runId === runId && s.run ? s : { runId, status: "loading", run: null }));
    void Promise.resolve()
      .then(() => getRun(runId))
      .then(
        (run) => {
          if (!cancelled) setState({ runId, status: "ok", run: run as RunTimes });
        },
        () => {
          if (!cancelled) setState((s) => (s.runId === runId && s.run ? s : { runId, status: "failed", run: null }));
        },
      );
    return () => {
      cancelled = true;
    };
  }, [runId, nonce]);
  if (!runId) return { status: "none", run: null };
  if (state.runId !== runId) return { status: "loading", run: null };
  return { status: state.status, run: state.run };
}

function LogsSearch() {
  const router = useRouter();
  const searchParams = useSearchParams();
  const urlKey = searchParams?.toString() ?? "";
  const filters = useMemo(() => parseLogFilters(new URLSearchParams(urlKey)), [urlKey]);

  // The filters as last written. Reset from the URL whenever the URL changes, and merged into by
  // every control, so a late write (the search debounce, Enter) never drops an earlier one whose
  // URL update has not rendered yet.
  const pendingRef = useRef<{ urlKey: string; filters: LogFilters }>({ urlKey, filters });
  if (pendingRef.current.urlKey !== urlKey) pendingRef.current = { urlKey, filters };
  const setFilters = useCallback(
    (patch: Partial<LogFilters>) => {
      const next = { ...pendingRef.current.filters, ...patch };
      pendingRef.current = { ...pendingRef.current, filters: next };
      const query = logFiltersToParams(next).toString();
      router.replace(query ? `/logs?${query}` : "/logs", { scroll: false });
    },
    [router],
  );

  // --- search text: debounced into the URL ---------------------------------------------------
  const [text, setText] = useState(filters.q ?? "");
  const committedRef = useRef(filters.q);
  const debounceRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  useEffect(() => {
    // Only an outside change (back/forward, a link) overwrites what is being typed.
    if (filters.q !== committedRef.current) {
      // A pending debounce holds the text typed before the change: drop it, or it re-applies.
      if (debounceRef.current) clearTimeout(debounceRef.current);
      debounceRef.current = null;
      committedRef.current = filters.q;
      setText(filters.q ?? "");
    }
  }, [filters.q]);
  /** The `q` patch for `value` (cancelling the pending debounce), or null when unchanged. */
  const textPatch = useCallback((value: string): Partial<LogFilters> | null => {
    if (debounceRef.current) clearTimeout(debounceRef.current);
    debounceRef.current = null;
    const next = value.trim() === "" ? null : value;
    if (next === committedRef.current) return null;
    committedRef.current = next;
    return { q: next };
  }, []);
  const commitText = useCallback(
    (value: string) => {
      const patch = textPatch(value);
      if (patch) setFilters(patch);
    },
    [textPatch, setFilters],
  );
  const onTextChange = (value: string) => {
    setText(value);
    if (debounceRef.current) clearTimeout(debounceRef.current);
    debounceRef.current = setTimeout(() => commitText(value), SEARCH_DEBOUNCE_MS);
  };
  useEffect(() => () => {
    if (debounceRef.current) clearTimeout(debounceRef.current);
  }, []);

  // --- run id: committed when it is a UUID, empty, or on blur/Enter ------------------------------
  const [runDraft, setRunDraft] = useState(filters.run_id ?? "");
  useEffect(() => setRunDraft(filters.run_id ?? ""), [filters.run_id]);
  const runPatch = (value: string): Partial<LogFilters> | null => {
    const next = value.trim() || null;
    return next !== pendingRef.current.filters.run_id ? { run_id: next } : null;
  };
  const commitRun = (value: string) => {
    const patch = runPatch(value);
    if (patch) setFilters(patch);
  };

  // --- agents and runs --------------------------------------------------------------------------
  const [agents, setAgents] = useState<Array<{ id: string; name: string }>>([]);
  useEffect(() => {
    let cancelled = false;
    void Promise.resolve()
      .then(() => api.agents.list())
      .then((list) => {
        if (!cancelled && Array.isArray(list)) {
          setAgents(list.map((a) => ({ id: a.id, name: a.name })).sort((a, b) => a.name.localeCompare(b.name)));
        }
      })
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, []);
  const [nonce, setNonce] = useState(0);
  const runOptions = useRunOptions(filters.agent_id, nonce);

  // --- the query --------------------------------------------------------------------------------
  const problem = logFiltersProblem(filters);
  // "Run lifetime": with a run and no range, the window is the run's own (loaded first; when it
  // cannot be loaded the API's default applies and answers 404 for a run that is not the tenant's).
  const autoRange = !filters.range && !filters.from && !filters.to;
  const selected = useSelectedRun(!problem && autoRange ? filters.run_id : null, nonce);
  const run = selected.status === "ok" ? selected.run : null;
  const runKey = run ? [run.created_at, run.finished_at ?? "", run.updated_at ?? "", run.display_status ?? ""].join("|") : "";
  // The agent is applied in the browser, so it is not part of the API query key.
  const apiKey = logFiltersToParams({ ...filters, agent_id: null }).toString();
  const search = useLogSearch(
    problem || selected.status === "loading" ? null : `${apiKey}#${runKey}#${nonce}`,
    () => logsRequestParams(filters, Date.now(), undefined, run),
  );
  const keep = useMemo(
    () => agentRunFilter({ ...EMPTY_LOG_FILTERS, agent_id: filters.agent_id, run_id: filters.run_id }, runOptions.agentRunIds),
    [filters.agent_id, filters.run_id, runOptions.agentRunIds],
  );
  const agentPending = !!keep && runOptions.agentRunIds === null && !runOptions.failed;
  const items = useMemo(() => (keep ? search.items.filter(keep) : search.items), [keep, search.items]);

  const rangeValue = filters.from || filters.to ? "custom" : (filters.range ?? "auto");
  const onRangeChange = (value: string) => {
    if (value === "custom") {
      const now = Date.now();
      setFilters({ range: null, from: new Date(now - 60 * 60 * 1000).toISOString(), to: new Date(now).toISOString() });
    } else {
      setFilters({ range: value === "auto" ? null : (value as RangePreset), from: null, to: null });
    }
  };

  const anyFilter = logFiltersToParams(filters).toString() !== "";
  const busy = search.loading || agentPending || selected.status === "loading";
  const status = problem
    ? ""
    : busy
      ? "Searching logs"
      : search.error
        ? ""
        : `${items.length} log record${items.length === 1 ? "" : "s"}${search.hasOlder ? ", older records available" : ""}`;

  return (
    <div className="mx-auto max-w-7xl px-4 py-8">
      <div className="mb-6">
        <h1 className="text-2xl font-bold text-card-foreground">Logs</h1>
        <p className="mt-1 text-muted-foreground">Search your API and worker logs, and jump from a record to its run&apos;s trace</p>
      </div>

      <form
        role="search"
        aria-label="Log filters"
        className="mb-4 flex flex-wrap items-end gap-3"
        onSubmit={(e) => {
          e.preventDefault();
          // One URL write for both, so neither overwrites the other.
          const patch = { ...textPatch(text), ...runPatch(runDraft) };
          if (Object.keys(patch).length) setFilters(patch);
        }}
      >
        <div className="flex min-w-64 flex-1 flex-col gap-1">
          <label htmlFor="logs-q" className="text-xs font-medium text-muted-foreground">Search messages</label>
          <input
            id="logs-q"
            type="search"
            value={text}
            maxLength={MAX_QUERY_TEXT}
            placeholder="Text in the message (case-sensitive)"
            onChange={(e) => onTextChange(e.target.value)}
            className={inputClass}
          />
        </div>
        <div className="flex flex-col gap-1">
          <label htmlFor="logs-level" className="text-xs font-medium text-muted-foreground">Level</label>
          <select
            id="logs-level"
            value={filters.level ?? ""}
            onChange={(e) => setFilters({ level: (e.target.value || null) as LogLevel | null })}
            className={inputClass}
          >
            <option value="">Any level</option>
            {LOG_LEVELS.map((level) => (
              <option key={level} value={level}>{LEVEL_LABELS[level]}</option>
            ))}
          </select>
        </div>
        <div className="flex flex-col gap-1">
          <label htmlFor="logs-agent" className="text-xs font-medium text-muted-foreground">Agent</label>
          <select
            id="logs-agent"
            value={filters.agent_id ?? ""}
            onChange={(e) => setFilters({ agent_id: e.target.value || null })}
            className={`${inputClass} max-w-48`}
          >
            <option value="">All agents</option>
            {agents.map((agent) => (
              <option key={agent.id} value={agent.id}>{agent.name}</option>
            ))}
            {filters.agent_id && !agents.some((a) => a.id === filters.agent_id) ? (
              <option value={filters.agent_id}>{filters.agent_id.slice(0, 8)}...</option>
            ) : null}
          </select>
        </div>
        <div className="flex flex-col gap-1">
          <label htmlFor="logs-run" className="text-xs font-medium text-muted-foreground">Run</label>
          <input
            id="logs-run"
            value={runDraft}
            list="logs-run-options"
            placeholder="Run id"
            spellCheck={false}
            aria-invalid={filters.run_id !== null && !isUuid(filters.run_id) ? true : undefined}
            onChange={(e) => {
              setRunDraft(e.target.value);
              if (e.target.value === "" || isUuid(e.target.value.trim())) commitRun(e.target.value);
            }}
            onBlur={() => commitRun(runDraft)}
            className={`${inputClass} w-72 font-mono`}
          />
          <datalist id="logs-run-options">
            {runOptions.runs.map((run) => (
              <option key={run.run_id} value={run.run_id} label={runOptionLabel(run)} />
            ))}
          </datalist>
        </div>
        <div className="flex flex-col gap-1">
          <label htmlFor="logs-range" className="text-xs font-medium text-muted-foreground">Time range</label>
          <select id="logs-range" value={rangeValue} onChange={(e) => onRangeChange(e.target.value)} className={inputClass}>
            <option value="auto">
              {!filters.run_id ? "Last hour (default)" : selected.status === "failed" ? "Since the run started" : "Run lifetime"}
            </option>
            {RANGE_PRESETS.map((p) => (
              <option key={p.value} value={p.value}>{p.label}</option>
            ))}
            <option value="custom">Custom</option>
          </select>
        </div>
        {rangeValue === "custom" ? (
          <>
            <div className="flex flex-col gap-1">
              <label htmlFor="logs-from" className="text-xs font-medium text-muted-foreground">From</label>
              <input
                id="logs-from"
                type="datetime-local"
                value={isoToLocalInput(filters.from)}
                onChange={(e) => setFilters({ from: localInputToIso(e.target.value) })}
                className={inputClass}
              />
            </div>
            <div className="flex flex-col gap-1">
              <label htmlFor="logs-to" className="text-xs font-medium text-muted-foreground">To</label>
              <input
                id="logs-to"
                type="datetime-local"
                value={isoToLocalInput(filters.to)}
                onChange={(e) => setFilters({ to: localInputToIso(e.target.value) })}
                className={inputClass}
              />
            </div>
          </>
        ) : null}
        <button type="button" className={buttonClass} onClick={() => setNonce((n) => n + 1)} disabled={search.loading || !!problem}>
          Refresh
        </button>
        {anyFilter ? (
          <button
            type="button"
            className={buttonClass}
            onClick={() => {
              if (debounceRef.current) clearTimeout(debounceRef.current);
              debounceRef.current = null;
              committedRef.current = null;
              pendingRef.current = { ...pendingRef.current, filters: EMPTY_LOG_FILTERS };
              setText("");
              router.replace("/logs", { scroll: false });
            }}
          >
            Clear filters
          </button>
        ) : null}
      </form>

      {problem ? (
        <p role="alert" data-testid="logs-filter-problem" className="mb-4 text-sm text-destructive-text">
          {problem}
        </p>
      ) : null}
      {keep ? (
        <p className="mb-3 text-xs text-muted-foreground" data-testid="logs-agent-note">
          {runOptions.failed
            ? "Could not load this agent's runs, so no records can be shown for it. Refresh to try again, or pick a run."
            : agentPending
            ? "Loading this agent's runs..."
            : `Showing records of this agent's ${runOptions.agentRunIds?.size ?? 0} most recent run${runOptions.agentRunIds?.size === 1 ? "" : "s"} (at most ${AGENT_RUNS_LIMIT}); pick a run to search all of its logs.`}
        </p>
      ) : null}

      <p className="sr-only" aria-live="polite" data-testid="logs-status">{status}</p>
      {!problem && !search.error && search.window ? (
        // The window the API actually searched: it may differ from the one asked for (a range
        // without `to` ends at the server's now, and one past 7 days is clamped to the last 7).
        <p className="mb-3 text-xs text-muted-foreground" data-testid="logs-window">
          Searched {formatWindowTime(search.window.from)} to {formatWindowTime(search.window.to)}
        </p>
      ) : null}

      {problem ? null : search.error ? (
        <LogsUnavailable error={search.error} onRetry={() => setNonce((n) => n + 1)} />
      ) : busy && items.length === 0 ? (
        <p className="py-12 text-center text-sm text-muted-foreground">Searching logs...</p>
      ) : items.length === 0 ? (
        <div className="rounded-md border border-border bg-card p-8 text-center" data-testid="logs-empty">
          <p className="font-medium text-card-foreground">{anyFilter ? "No logs match these filters" : "No logs in the last hour"}</p>
          <p className="mt-1 text-sm text-muted-foreground">
            {search.hasOlder
              ? "None of the records on this page matched; load older records below."
              : anyFilter
                ? "Try a wider time range, a lower level, or different search text."
                : "Records appear here once the API or a worker logs inside a run."}
          </p>
        </div>
      ) : (
        <LogsTable items={items} caption="Log records, newest first" busy={busy} />
      )}

      {!problem && !search.error && search.hasOlder ? (
        <div className="mt-4 flex justify-center">
          <button type="button" className={buttonClass} onClick={search.loadOlder} disabled={search.loadingOlder || search.loading}>
            {search.loadingOlder ? "Loading..." : "Load older"}
          </button>
        </div>
      ) : null}
      {search.olderError ? (
        <p role="alert" className="mt-2 text-center text-sm text-muted-foreground">{search.olderError}</p>
      ) : null}
    </div>
  );
}

export default function LogsPage() {
  return (
    <Suspense fallback={<p className="py-8 text-center text-sm text-muted-foreground">Loading...</p>}>
      <LogsSearch />
    </Suspense>
  );
}
