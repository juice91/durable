"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { getRun, subscribeRun } from "@/lib/runs";
import { getEvalRun, isFinishedStatus, listAllResults, type EvalCaseResult, type EvalRun, type EvalTargetKind } from "@/lib/evals";

export interface EvalRunState {
  run: EvalRun | null;
  results: EvalCaseResult[] | null;
  /** Cases in the run's frozen suite snapshot (null until known). */
  total: number | null;
  error: string | null;
  reload: () => void;
}

/** Cases in a run's suite snapshot (`runs.input`, canonical JSON); null when unreadable. */
export function snapshotCaseCount(input: string | null | undefined): number | null {
  if (!input) return null;
  try {
    const cases = (JSON.parse(input) as { cases?: unknown }).cases;
    return Array.isArray(cases) ? cases.length : null;
  } catch {
    return null;
  }
}

/**
 * Case counts of eval runs started in this tab (the start response says how many cases were frozen),
 * so the progress bar does not have to read the run's whole suite snapshot (`GET /runs/{id}`, which
 * can be megabytes) to count them.
 */
const knownCaseCounts = new Map<string, number>();

export function rememberCaseCount(runId: string, cases: number): void {
  knownCaseCounts.set(runId, cases);
}

/** Minimum time between two live re-reads while the run is going: a burst of finished cases costs
 * one re-read per interval, not one per case. The first event, and the run's end, read at once. */
export const LIVE_REFRESH_INTERVAL_MS = 750;

/**
 * An eval run and its results, kept live while it runs: the run's SSE stream's `eval_case_result`
 * events (one per finished case) and its end trigger a re-read of the run (aggregates) and its
 * results. Re-reads never overlap (one more follows when events arrived meanwhile), are spaced by
 * LIVE_REFRESH_INTERVAL_MS while running, and belong to one run id: a read for an earlier run id
 * is dropped. `onFinished` is called once when a run seen running reaches a finished status.
 */
export function useEvalRun(agentId: string, runId: string, onFinished?: () => void, kind: EvalTargetKind = "agent"): EvalRunState {
  const [run, setRun] = useState<EvalRun | null>(null);
  const [results, setResults] = useState<EvalCaseResult[] | null>(null);
  const [total, setTotal] = useState<number | null>(() => knownCaseCounts.get(runId) ?? null);
  const [error, setError] = useState<string | null>(null);
  const [reloadKey, setReloadKey] = useState(0);
  const inFlight = useRef(false);
  const again = useRef(false);
  /** Bumped per run id and reload: a loop of an older generation stops and its data is dropped. */
  const generation = useRef(0);
  const lastStart = useRef(0);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const sawRunning = useRef(false);
  const onFinishedRef = useRef(onFinished);
  onFinishedRef.current = onFinished;

  const read = useCallback(async () => {
    const gen = generation.current;
    if (inFlight.current) {
      again.current = true;
      return;
    }
    inFlight.current = true;
    try {
      do {
        again.current = false;
        lastStart.current = Date.now();
        try {
          const [loaded, items] = await Promise.all([getEvalRun(agentId, runId, undefined, kind), listAllResults(agentId, runId, undefined, kind)]);
          if (gen !== generation.current) return;
          setRun(loaded);
          setResults(items);
          setError(null);
          if (!isFinishedStatus(loaded.status)) sawRunning.current = true;
          else if (sawRunning.current) {
            sawRunning.current = false;
            onFinishedRef.current?.();
          }
        } catch (reason: unknown) {
          if (gen !== generation.current) return;
          setError(reason instanceof Error && reason.message ? reason.message : "The eval run could not be loaded");
        }
      } while (again.current && gen === generation.current);
    } finally {
      // A newer generation's read may have started meanwhile; it owns the flag then.
      if (gen === generation.current) inFlight.current = false;
    }
  }, [agentId, runId, kind]);

  /** A live re-read: at once when the last one started long enough ago, else once at the interval's end. */
  const refresh = useCallback(
    (now = false) => {
      if (timer.current !== null) {
        if (!now) return;
        clearTimeout(timer.current);
        timer.current = null;
      }
      const wait = now ? 0 : Math.max(0, lastStart.current + LIVE_REFRESH_INTERVAL_MS - Date.now());
      if (wait === 0) {
        void read();
        return;
      }
      timer.current = setTimeout(() => {
        timer.current = null;
        void read();
      }, wait);
    },
    [read],
  );

  useEffect(() => {
    generation.current += 1;
    inFlight.current = false;
    again.current = false;
    lastStart.current = 0;
    sawRunning.current = false;
    setRun(null);
    setResults(null);
    setTotal(knownCaseCounts.get(runId) ?? null);
    setError(null);
    void read();
    return () => {
      generation.current += 1;
      if (timer.current !== null) clearTimeout(timer.current);
      timer.current = null;
    };
  }, [read, reloadKey, runId]);

  const finished = run ? isFinishedStatus(run.status) : null;

  // The case count of the frozen snapshot, once, for the progress bar (unless this tab started it).
  useEffect(() => {
    if (finished !== false || total !== null) return;
    let cancelled = false;
    getRun(runId)
      .then((full) => {
        if (cancelled) return;
        const count = snapshotCaseCount(full.input);
        if (count !== null) rememberCaseCount(runId, count);
        setTotal(count);
      })
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, [finished, runId, total]);

  // Live while running.
  useEffect(() => {
    if (finished !== false) return;
    let unsubscribe: (() => void) | null = null;
    try {
      unsubscribe = subscribeRun(runId, {
        onEvent: (event) => {
          if (event.type === "eval_case_result") refresh();
          else if (event.type === "run_completed" || event.type === "run_failed") refresh(true);
        },
        onStatus: (status) => {
          if (isFinishedStatus(status.status)) refresh(true);
        },
        onEnd: () => refresh(true),
      });
    } catch {
      // No credentials (a misconfigured Studio): the page still shows what it read.
    }
    return () => unsubscribe?.();
  }, [finished, refresh, runId]);

  return { run, results, total, error, reload: () => setReloadKey((k) => k + 1) };
}
