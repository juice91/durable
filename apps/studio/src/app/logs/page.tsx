"use client";

import React, { Suspense, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useSearchParams } from "next/navigation";
import { api } from "@/lib/api";
import { getRun } from "@/lib/runs";
import { isoToLocalInput, localInputToIso } from "@/lib/datetimeLocal";
import { EMPTY_FILTERS, listTraceRuns, type TraceRun } from "@/lib/traces";
import { LogsList } from "@/components/logs/LogsList";
import { LogSheet } from "@/components/logs/LogSheet";
import { TraceSheetHost } from "@/components/traces/TraceSheetHost";
import { usePendingQuery } from "@/components/traces/usePendingQuery";
import { TRACE_PARAM } from "@/lib/routes/observability";
import { LogsUnavailable } from "@/components/logs/LogsUnavailable";
import { useLogSearch } from "@/components/logs/useLogSearch";
import {
  AGENT_RUNS_LIMIT,
  EMPTY_LOG_FILTERS,
  LOG_LEVELS,
  SEARCH_DEBOUNCE_MS,
  MAX_QUERY_TEXT,
  RANGE_PRESETS,
  runOptionLabel,
  isUuid,
  logFiltersProblem,
  logFiltersToParams,
  logsRequestParams,
  parseLogFilters,
  type LogFilters,
  type LogItem,
  type LogLevel,
  type RangePreset,
  type RunTimes,
} from "@/components/logs/logsQuery";
import { routes } from "@/lib/routes";
import { PageHeader } from "@/components/frame/PageHeader";
import { navCrumb } from "@/lib/nav";

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

interface RunOptions {
  runs: TraceRun[];
  loading: boolean;
  failed: boolean;
}

/** The run picker's suggestions: the agent's most recent runs, or recent runs of any kind.
 * Re-fetched on Refresh (`nonce`); a response for a previous agent is dropped. */
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
  return { runs: current ? state.runs : [], loading: !current || state.loading, failed: current && state.failed };
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
  const searchParams = useSearchParams();
  const urlKey = searchParams?.toString() ?? "";
  const filters = useMemo(() => parseLogFilters(new URLSearchParams(urlKey)), [urlKey]);

  // The filters as last written, ahead of the URL while a write is in flight (usePendingQuery): a
  // late write (the search debounce, Enter) never drops an earlier one, and the trace Sheet opens on
  // top of a run filter committed on blur by the click that opens it.
  const { setFilters, writeFilters, pendingFilters, pendingParams } = usePendingQuery<LogFilters>({
    filters,
    toParams: logFiltersToParams,
    href: routes.logs,
  });

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
    return next !== pendingFilters().run_id ? { run_id: next } : null;
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
  // The record whose log Sheet is open (no URL state: records have no stable id).
  const [openLog, setOpenLog] = useState<{ item: LogItem; key: string } | null>(null);
  // Opening a Sheet (a record, or a trace: `?trace=`) cancels a pending search debounce: its late
  // URL write must not land under the Sheet. The typed text stays in the box; Enter applies it.
  const cancelSearchDebounce = useCallback(() => {
    if (debounceRef.current) clearTimeout(debounceRef.current);
    debounceRef.current = null;
  }, []);
  const traceOpen = searchParams?.get(TRACE_PARAM) ?? null;
  useEffect(() => {
    if (traceOpen) cancelSearchDebounce();
  }, [traceOpen, cancelSearchDebounce]);
  const onOpenLog = useCallback(
    (item: LogItem, key: string) => {
      cancelSearchDebounce();
      setOpenLog({ item, key });
    },
    [cancelSearchDebounce],
  );
  const runOptions = useRunOptions(filters.agent_id, nonce);

  // --- the query --------------------------------------------------------------------------------
  const problem = logFiltersProblem(filters);
  // "Run lifetime": with a run and no range, the window is the run's own (loaded first; when it
  // cannot be loaded the API's default applies and answers 404 for a run that is not the tenant's).
  const autoRange = !filters.range && !filters.from && !filters.to;
  const selected = useSelectedRun(!problem && autoRange ? filters.run_id : null, nonce);
  const run = selected.status === "ok" ? selected.run : null;
  const runKey = run ? [run.created_at, run.finished_at ?? "", run.updated_at ?? "", run.display_status ?? ""].join("|") : "";
  // The agent filter is the API's own (`agent_id`): it is part of the query key like the rest.
  const apiKey = logFiltersToParams(filters).toString();
  const search = useLogSearch(
    problem || selected.status === "loading" ? null : `${apiKey}#${runKey}#${nonce}`,
    () => logsRequestParams(filters, Date.now(), undefined, run),
  );
  const items = search.items;
  const agentRuns = filters.agent_id && !filters.run_id && !search.loading && !search.error ? search.agentRuns : null;

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
  const busy = search.loading || selected.status === "loading";
  const status = problem
    ? ""
    : busy
      ? "Searching logs"
      : search.error
        ? ""
        : `${items.length} log record${items.length === 1 ? "" : "s"}${search.hasOlder ? ", older records available" : ""}`;

  return (
    <>
      <PageHeader crumbs={[navCrumb("logs", { current: true })]} />
    <TraceSheetHost baseParams={pendingParams}>
    <div className="mx-auto max-w-7xl px-4 py-8">
      <div className="mb-6">
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
              setText("");
              writeFilters(EMPTY_LOG_FILTERS);
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
      {agentRuns ? (
        <p className="mb-3 text-xs text-muted-foreground" data-testid="logs-agent-note">
          {agentRuns.count === 0
            ? "This agent has no runs in this time range."
            : agentRuns.limited
              ? `Searched this agent's ${agentRuns.count} most recent runs in this time range; pick a run to search an older one.`
              : `Searched this agent's ${agentRuns.count} run${agentRuns.count === 1 ? "" : "s"} in this time range.`}
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
        <LogsUnavailable error={search.error} onRetry={() => setNonce((n) => n + 1)} onClearAgent={() => setFilters({ agent_id: null })} />
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
        <LogsList items={items} caption="Log records, newest first" busy={busy} selectedKey={openLog?.key ?? null} onOpen={onOpenLog} />
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
      <LogSheet item={openLog?.item ?? null} onClose={() => setOpenLog(null)} />
    </div>
    </TraceSheetHost>
    </>
  );
}

export default function LogsPage() {
  return (
    <Suspense fallback={<><PageHeader crumbs={[navCrumb("logs", { current: true })]} /><p className="py-8 text-center text-sm text-muted-foreground">Loading...</p></>}>
      <LogsSearch />
    </Suspense>
  );
}
