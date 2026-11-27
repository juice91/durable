"use client";

import React, { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { Button } from "@/components/ui/Button";
import { routes } from "@/lib/routes";
import {
  attachScorer,
  describeEvaluator,
  detachScorer,
  listAgentScorers,
  listReusableScorers,
  type AttachedScorer,
  type ReusableScorer,
} from "@/lib/scorers";
import { Select } from "@/components/ui/Select";

/**
 * The reusable scorers attached to an agent (self-contained Config section): attach one from the
 * tenant's scorers with a sampling rate, or detach it. The worker applies them to every finished run
 * of the agent (the draft chat included is not scored) and records `score` events.
 */
export function AttachedScorers({ agentId, mayEdit = true }: { agentId: string; mayEdit?: boolean }) {
  const [attached, setAttached] = useState<AttachedScorer[] | null>(null);
  const [available, setAvailable] = useState<ReusableScorer[]>([]);
  const [choice, setChoice] = useState("");
  const [rate, setRate] = useState("100");
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async (signal?: AbortSignal) => {
    try {
      const [mine, all] = await Promise.all([listAgentScorers(agentId, signal), listReusableScorers(signal)]);
      setAttached(mine);
      setAvailable(all);
      setError(null);
    } catch (err) {
      if (signal?.aborted) return;
      setAttached((current) => current ?? []);
      setError(err instanceof Error && err.message ? err.message : "Could not load scorers");
    }
  }, [agentId]);

  useEffect(() => {
    const controller = new AbortController();
    void load(controller.signal);
    return () => controller.abort();
  }, [load]);

  const attachedIds = new Set((attached ?? []).map((s) => s.id));
  const options = available.filter((s) => !attachedIds.has(s.id));

  const attach = async (event: React.FormEvent) => {
    event.preventDefault();
    const percent = Number(rate);
    if (!choice) return setError("Choose a scorer.");
    if (!(percent > 0 && percent <= 100)) return setError("The sampling rate is a percentage above 0 and up to 100.");
    try {
      setAttached(await attachScorer(agentId, choice, percent / 100));
      setChoice("");
      setError(null);
    } catch (err) {
      setError(err instanceof Error && err.message ? err.message : "Could not attach the scorer");
    }
  };

  const detach = async (id: string) => {
    try {
      await detachScorer(agentId, id);
      setAttached((current) => (current ?? []).filter((s) => s.id !== id));
      setError(null);
    } catch (err) {
      setError(err instanceof Error && err.message ? err.message : "Could not detach the scorer");
    }
  };

  return (
    <div data-testid="attached-scorers" className="flex min-w-0 flex-col gap-2">
      {attached === null ? <p className="text-caption text-muted-foreground">Loading…</p> : null}
      {attached && attached.length === 0 ? <p className="text-caption text-muted-foreground">No scorers attached.</p> : null}
      {attached && attached.length > 0 ? (
        <ul className="flex flex-col gap-1">
          {attached.map((s) => (
            <li key={s.id} data-testid="attached-scorer" className="flex min-w-0 flex-wrap items-center gap-x-2">
              <span className="font-mono text-caption">{s.name}</span>
              <span className="min-w-0 flex-1 truncate text-caption text-muted-foreground">
                {describeEvaluator(s.evaluator)}, {Math.round(s.sampling_rate * 100)}% of runs
              </span>
              {mayEdit ? <Button size="sm" variant="secondary" aria-label={`Detach scorer ${s.name}`} onClick={() => void detach(s.id)}>Detach</Button> : null}
            </li>
          ))}
        </ul>
      ) : null}
      {!mayEdit ? null : options.length > 0 ? (
        <form onSubmit={attach} aria-label="Attach a scorer" className="flex flex-wrap items-end gap-2">
          <label className="flex min-w-0 flex-col gap-1 text-caption text-muted-foreground">
            Scorer
            <Select value={choice} onValueChange={(v) => setChoice(v)} className="rounded-md border border-input bg-surface px-2 py-1 text-caption text-foreground">
              <option value="">Choose…</option>
              {options.map((s) => <option key={s.id} value={s.id}>{s.name}</option>)}
            </Select>
          </label>
          <label className="flex w-20 flex-col gap-1 text-caption text-muted-foreground">
            Sample %
            <input value={rate} onChange={(e) => setRate(e.target.value)} inputMode="decimal" className="rounded-md border border-input bg-surface px-2 py-1 text-caption text-foreground" />
          </label>
          <Button type="submit" size="sm">Attach</Button>
        </form>
      ) : attached && available.length === 0 ? (
        <p className="text-caption text-muted-foreground">
          Create scorers on the <Link href={routes.scorers()} className="underline underline-offset-4">Scorers page</Link>.
        </p>
      ) : null}
      {error ? <p role="alert" className="text-caption text-destructive-text">{error}</p> : null}
    </div>
  );
}
