"use client";

import React, { useCallback, useEffect, useState } from "react";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { toast } from "@/components/ui/Toaster";
import {
  createScorer,
  deleteScorer,
  describeEvaluator,
  evaluatorFromForm,
  listReusableScorers,
  SCORER_KIND_LABELS,
  SCORER_KINDS,
  type ReusableScorer,
  type ScorerKind,
} from "@/lib/scorers";

const field =
  "rounded-md border border-input bg-surface px-2 py-1.5 text-label text-foreground placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring";

const PRIMARY: Record<ScorerKind, { label: string; placeholder: string } | null> = {
  contains: { label: "Text", placeholder: "refund" },
  regex: { label: "Pattern", placeholder: "^[A-Z]" },
  schema_valid: null,
  field_equals: { label: "Field path", placeholder: "status" },
  max_cost: { label: "Max cost (USD)", placeholder: "0.05" },
};

/**
 * Reusable scorers: named checks the worker applies to the finished runs of the agents they are
 * attached to (attach them in an agent's Config panel). Scores appear in the trace's Scores tab.
 */
export function ReusableScorers() {
  const [items, setItems] = useState<ReusableScorer[] | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [name, setName] = useState("");
  const [kind, setKind] = useState<ScorerKind>("contains");
  const [primary, setPrimary] = useState("");
  const [secondary, setSecondary] = useState("");
  const [negate, setNegate] = useState(false);
  const [regexField, setRegexField] = useState("");
  const [formError, setFormError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  const load = useCallback(async (signal?: AbortSignal) => {
    try {
      setItems(await listReusableScorers(signal));
      setLoadError(null);
    } catch (err) {
      if (signal?.aborted) return;
      setItems([]);
      setLoadError(err instanceof Error && err.message ? err.message : "Could not load scorers");
    }
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    void load(controller.signal);
    return () => controller.abort();
  }, [load]);

  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    if (!name.trim()) return setFormError("Enter a name.");
    const evaluator = evaluatorFromForm(kind, primary, secondary, { negate, field: regexField });
    if (typeof evaluator === "string") return setFormError(evaluator);
    setSaving(true);
    setFormError(null);
    try {
      await createScorer({ name: name.trim(), evaluator });
      setName("");
      setPrimary("");
      setSecondary("");
      setNegate(false);
      setRegexField("");
      await load();
    } catch (err) {
      setFormError(err instanceof Error && err.message ? err.message : "Could not save the scorer");
    } finally {
      setSaving(false);
    }
  };

  const remove = async (scorer: ReusableScorer) => {
    try {
      await deleteScorer(scorer.id);
      await load();
    } catch (err) {
      toast(err instanceof Error && err.message ? err.message : "Could not delete the scorer");
    }
  };

  const primaryField = PRIMARY[kind];
  return (
    <section aria-labelledby="reusable-scorers-title" data-testid="reusable-scorers" className="flex min-w-0 flex-col gap-3 rounded-md border border-border p-4">
      <div>
        <h2 id="reusable-scorers-title" className="text-label font-semibold text-foreground">Reusable scorers</h2>
        <p className="text-caption text-muted-foreground">
          Checks applied to an agent&apos;s finished runs once you attach them in its Config panel. Each result is recorded on the run and shown in its trace&apos;s Scores tab.
        </p>
      </div>

      {loadError ? <p role="status" data-testid="reusable-scorers-error" className="text-label text-destructive-text">{loadError}</p> : null}
      {items && items.length > 0 ? (
        <ul className="flex flex-col divide-y divide-border" data-testid="reusable-scorers-list">
          {items.map((s) => (
            <li key={s.id} data-testid="reusable-scorer" className="flex min-w-0 flex-wrap items-center gap-x-3 gap-y-1 py-2">
              <span className="font-mono text-label">{s.name}</span>
              <Badge variant="outline">{SCORER_KIND_LABELS[s.evaluator.kind]}</Badge>
              <span className="min-w-0 flex-1 truncate text-caption text-muted-foreground">{describeEvaluator(s.evaluator)}</span>
              <span className="text-caption text-muted-foreground">{s.agent_ids.length} {s.agent_ids.length === 1 ? "agent" : "agents"}</span>
              <Button size="sm" variant="secondary" aria-label={`Delete scorer ${s.name}`} onClick={() => void remove(s)}>Delete</Button>
            </li>
          ))}
        </ul>
      ) : items ? (
        <p className="text-label text-muted-foreground" data-testid="reusable-scorers-empty">No reusable scorers yet.</p>
      ) : null}

      <form onSubmit={submit} className="flex flex-wrap items-end gap-3" aria-label="New scorer">
        <label className="flex flex-col gap-1 text-caption text-muted-foreground">
          Name
          <input className={field} value={name} onChange={(e) => setName(e.target.value)} maxLength={100} />
        </label>
        <label className="flex flex-col gap-1 text-caption text-muted-foreground">
          Check
          <select className={field} value={kind} onChange={(e) => setKind(e.target.value as ScorerKind)}>
            {SCORER_KINDS.map((k) => <option key={k} value={k}>{SCORER_KIND_LABELS[k]}</option>)}
          </select>
        </label>
        {primaryField ? (
          <label className="flex flex-col gap-1 text-caption text-muted-foreground">
            {primaryField.label}
            <input className={field} value={primary} placeholder={primaryField.placeholder} onChange={(e) => setPrimary(e.target.value)} />
          </label>
        ) : null}
        {kind === "regex" ? (
          <>
            <label className="flex items-center gap-2 text-caption text-muted-foreground">
              <input type="checkbox" checked={negate} onChange={(e) => setNegate(e.target.checked)} />
              Must not match
            </label>
            <label className="flex flex-col gap-1 text-caption text-muted-foreground">
              Field path (optional)
              <input className={field} value={regexField} placeholder="post" onChange={(e) => setRegexField(e.target.value)} />
            </label>
          </>
        ) : null}
        {kind === "field_equals" ? (
          <label className="flex flex-col gap-1 text-caption text-muted-foreground">
            Value (JSON)
            <input className={field} value={secondary} placeholder={'"ok"'} onChange={(e) => setSecondary(e.target.value)} />
          </label>
        ) : null}
        <Button type="submit" size="sm" disabled={saving}>Add scorer</Button>
        {formError ? <p role="alert" className="w-full text-label text-destructive-text">{formError}</p> : null}
      </form>
    </section>
  );
}
