"use client";

import React, { useEffect, useState } from "react";
import { Button } from "@/components/ui/Button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/Dialog";
import {
  describeEvaluator,
  listAgentScorers,
  listReusableScorers,
  listRunScores,
  scoreRun,
  type ReusableScorer,
} from "@/lib/scorers";

/** What a request tells the Scores tab, so it knows when the new scores have arrived. */
export interface ScoreRequest {
  /** How many scorers were queued. */
  count: number;
  /** How many scores the run had when the request was made. */
  baseline: number;
}

export interface ScoreTraceDialogProps {
  runId: string;
  /** The run's agent: its attached scorers are preselected. Null for a run without one. */
  agentId: string | null;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onRequested: (request: ScoreRequest) => void;
}

/**
 * Score a finished run now: pick scorers from the tenant's list. Scorers attached to the run's
 * agent are preselected unless they already scored the run; those that did are checked, disabled
 * and marked Scored (the API skips them, and refuses a request made only of them).
 */
export function ScoreTraceDialog({ runId, agentId, open, onOpenChange, onRequested }: ScoreTraceDialogProps) {
  const [scorers, setScorers] = useState<ReusableScorer[] | null>(null);
  const [scoredIds, setScoredIds] = useState<ReadonlySet<string>>(new Set());
  const [baseline, setBaseline] = useState(0);
  const [chosen, setChosen] = useState<ReadonlySet<string>>(new Set());
  const [loadError, setLoadError] = useState<string | null>(null);
  const [submitError, setSubmitError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  useEffect(() => {
    if (!open) return;
    const controller = new AbortController();
    setScorers(null);
    setLoadError(null);
    setSubmitError(null);
    void (async () => {
      try {
        const [all, attached, scores] = await Promise.all([
          listReusableScorers(controller.signal),
          // Attachments only preselect: without them the picker still works.
          agentId ? listAgentScorers(agentId, controller.signal).catch(() => []) : Promise.resolve([]),
          listRunScores(runId, controller.signal),
        ]);
        if (controller.signal.aborted) return;
        const done = new Set(scores.map((s) => s.scorer_id));
        setScorers(all);
        setScoredIds(done);
        setBaseline(scores.length);
        setChosen(new Set(attached.map((a) => a.id).filter((id) => !done.has(id))));
      } catch (err) {
        if (controller.signal.aborted) return;
        setLoadError(err instanceof Error && err.message ? err.message : "Could not load the scorers");
      }
    })();
    return () => controller.abort();
  }, [open, runId, agentId]);

  const toggle = (id: string) =>
    setChosen((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });

  const ids = (scorers ?? []).map((s) => s.id).filter((id) => chosen.has(id) && !scoredIds.has(id));

  const submit = async () => {
    setSubmitting(true);
    setSubmitError(null);
    try {
      await scoreRun(runId, ids);
      onRequested({ count: ids.length, baseline });
      onOpenChange(false);
    } catch (err) {
      setSubmitError(err instanceof Error && err.message ? err.message : "Could not start scoring");
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Score trace</DialogTitle>
          <DialogDescription>Run the chosen scorers on this run now. Results appear on the Scores tab.</DialogDescription>
        </DialogHeader>
        {loadError ? (
          <p role="alert" className="text-sm text-destructive-text">{loadError}</p>
        ) : scorers === null ? (
          <p className="text-sm text-muted-foreground">Loading scorers...</p>
        ) : scorers.length === 0 ? (
          <p className="text-sm text-muted-foreground">No scorers yet. Create one on the Scorers page.</p>
        ) : (
          <ul aria-label="Scorers" className="flex max-h-72 min-w-0 flex-col divide-y divide-border overflow-y-auto rounded-md border border-border">
            {scorers.map((scorer) => {
              const done = scoredIds.has(scorer.id);
              return (
                <li key={scorer.id}>
                  <label className="flex min-w-0 cursor-pointer items-start gap-2 px-3 py-2 text-label">
                    <input
                      type="checkbox"
                      className="mt-0.5"
                      checked={done || chosen.has(scorer.id)}
                      disabled={done}
                      onChange={() => toggle(scorer.id)}
                    />
                    <span className="min-w-0 flex-1">
                      <span className="block truncate font-medium">{scorer.name}</span>
                      <span className="block truncate text-caption text-muted-foreground">{describeEvaluator(scorer.evaluator)}</span>
                    </span>
                    {done ? <span className="text-caption text-muted-foreground">Scored</span> : null}
                  </label>
                </li>
              );
            })}
          </ul>
        )}
        {submitError ? <p role="alert" className="text-sm text-destructive-text">{submitError}</p> : null}
        <DialogFooter>
          <Button variant="ghost" onClick={() => onOpenChange(false)}>Cancel</Button>
          <Button onClick={() => void submit()} disabled={submitting || ids.length === 0}>Score</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
