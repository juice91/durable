"use client";

import React, { useCallback, useEffect, useRef, useState } from "react";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { listRunScores, type ScoreEvent } from "@/lib/scorers";

/** How often, and for how long, the tab polls after a score request. */
export const SCORE_POLL_MS = 3000;
export const SCORE_POLL_WINDOW_MS = 60_000;

/** A score request the tab is waiting on: `key` distinguishes requests; see ScoreRequest. */
export interface PendingScores {
  key: number;
  count: number;
  baseline: number;
}

/**
 * The Scores tab of a trace: the `score` events the worker recorded for the run after it finished
 * (one per attached scorer that sampled it): scorer, pass or fail, score and the reason.
 */
export function TraceScores({ runId, requested = null }: { runId: string; requested?: PendingScores | null }) {
  const [scores, setScores] = useState<ScoreEvent[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async (signal?: AbortSignal) => {
    try {
      setScores(await listRunScores(runId, signal));
      setError(null);
    } catch (err) {
      if (signal?.aborted) return;
      setError(err instanceof Error && err.message ? err.message : "Could not load scores");
    }
  }, [runId]);

  const [timedOut, setTimedOut] = useState(false);
  const scoresRef = useRef<ScoreEvent[] | null>(null);
  scoresRef.current = scores;

  // After a request: reload every SCORE_POLL_MS until the new scores are in or SCORE_POLL_WINDOW_MS pass.
  useEffect(() => {
    setTimedOut(false);
    if (!requested) return;
    const started = Date.now();
    const timer = setInterval(() => {
      const have = scoresRef.current?.length ?? 0;
      if (have >= requested.baseline + requested.count) {
        clearInterval(timer);
        return;
      }
      if (Date.now() - started >= SCORE_POLL_WINDOW_MS) {
        clearInterval(timer);
        setTimedOut(true);
        return;
      }
      void load();
    }, SCORE_POLL_MS);
    return () => clearInterval(timer);
  }, [requested, load]);

  useEffect(() => {
    setScores(null);
    setError(null);
    const controller = new AbortController();
    void load(controller.signal);
    return () => controller.abort();
  }, [load]);

  if (error) {
    return (
      <div role="alert" data-testid="trace-scores-error" className="flex flex-wrap items-center justify-between gap-3 rounded-md border border-destructive/40 bg-destructive/10 p-3 text-sm text-destructive-text">
        <span className="min-w-0">{error}</span>
        <Button size="sm" variant="secondary" onClick={() => void load()}>Retry</Button>
      </div>
    );
  }
  const slowNotice = timedOut ? (
      <p role="status" data-testid="trace-scores-slow" className="mb-3 text-sm text-muted-foreground">
        Scoring is taking longer than expected{" "}
        <button type="button" className="text-info-text underline-offset-4 hover:underline" onClick={() => { setTimedOut(false); void load(); }}>Refresh</button>
      </p>
    ) : null;
  if (scores === null) return <p className="py-8 text-center text-sm text-muted-foreground">Loading scores...</p>;
  if (scores.length === 0) {
    return (
      <>
        {slowNotice}
        <p data-testid="trace-scores-empty" className="py-8 text-center text-sm text-muted-foreground">
          No scores for this run. Attach scorers to the agent to score its runs; scores appear here shortly after a run finishes.
        </p>
      </>
    );
  }
  return (
    <>
    {slowNotice}
    <ul data-testid="trace-scores" aria-label="Scores" className="flex min-w-0 flex-col divide-y divide-border rounded-md border border-border">
      {scores.map((s) => (
        <li key={s.scorer_id || s.seq} data-testid="trace-score" className="flex min-w-0 flex-wrap items-center gap-x-3 gap-y-1 px-3 py-2">
          <span className="min-w-0 flex-1 truncate font-mono text-label" title={s.scorer}>{s.scorer}</span>
          <Badge variant={s.passed ? "success" : "destructive"}>{s.passed ? "Passed" : "Failed"}</Badge>
          <span className="tabular-nums text-label text-muted-foreground" title="Score">{s.score === null ? "no score" : s.score.toFixed(2)}</span>
          {s.reason ? <span className="w-full text-caption text-muted-foreground">{s.reason}</span> : null}
        </li>
      ))}
    </ul>
    </>
  );
}
