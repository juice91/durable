"use client";

import React, { createContext, useContext, useId, useState } from "react";
import type { ArchitectOptions } from "@/lib/architect";
import { Select } from "@/components/ui/Select";

/** The model of the built-in Architect agents and the way to change it (PUT /architect/model). */
export interface ArchitectModelValue {
  model: string | null;
  options: ArchitectOptions | null;
  /** Sets the model on the three built-in agents and refreshes the page; rejects with the API's message. */
  change: (model: string) => Promise<void>;
}

const ArchitectModelContext = createContext<ArchitectModelValue | null>(null);
export const ArchitectModelProvider = ArchitectModelContext.Provider;
export const useArchitectModel = () => useContext(ArchitectModelContext);

/** A select over the allowed models that applies the choice at once; `label` is its accessible name. */
export function ModelPicker({ label }: { label: string }) {
  const value = useArchitectModel();
  const id = useId();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [applied, setApplied] = useState<string | null>(null);
  const models = value?.options?.models ?? [];
  if (!value || models.length === 0) return null;
  const ids = models.map((m) => m.id);
  if (value.model && !ids.includes(value.model)) ids.unshift(value.model);

  async function choose(next: string) {
    if (!value || next === value.model) return;
    setBusy(true);
    setError(null);
    setApplied(null);
    try {
      await value.change(next);
      setApplied(next);
    } catch (reason: unknown) {
      setError(reason instanceof Error ? reason.message : "The model could not be changed");
    } finally {
      setBusy(false);
    }
  }

  return (
    <span className="inline-flex flex-wrap items-center gap-2">
      <Select
        id={id}
        aria-label={label}
        disabled={busy}
        value={value.model ?? ""}
        onValueChange={(v) => void choose(v)}
        className="rounded-md border border-input bg-surface px-2 py-1 text-label text-foreground focus:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:opacity-50"
      >
        {value.model ? null : <option value="">Choose a model</option>}
        {ids.map((model) => <option key={model} value={model}>{model}</option>)}
      </Select>
      {applied ? <span className="text-caption text-muted-foreground">Model set to {applied}. Fill in the form again.</span> : null}
      {error ? <span role="alert" className="text-caption text-destructive-text">{error}</span> : null}
    </span>
  );
}

/**
 * What the Form and Workflow tabs show when the drafter answered but not with a usable form (422
 * `draft_invalid`) or its run failed (502 `draft_failed`): one sentence, the picker, and what the drafter
 * answered kept collapsed. The server's own message and the run's retry text are never shown outside it.
 */
export function DrafterFailure({ answer, problems = [] }: { answer: string | null; problems?: readonly string[] }) {
  const shown = answer && answer.trim() ? answer : null;
  return (
    <div role="alert" data-testid="architect-drafter-failure" className="space-y-2 rounded-md border border-destructive/40 bg-destructive/10 p-3 text-label text-destructive-text">
      <p>The drafter&apos;s model did not produce the form. Choose another model.</p>
      <ModelPicker label="Drafter model" />
      {shown || problems.length > 0 ? (
        <details className="text-caption">
          <summary className="cursor-pointer underline underline-offset-4">What the drafter answered</summary>
          {shown ? <pre className="mt-1 max-h-48 overflow-auto whitespace-pre-wrap break-words rounded border border-border bg-surface p-2 text-foreground">{shown}</pre> : null}
          {problems.length > 0 ? <ul className="mt-1 list-inside list-disc">{problems.map((p) => <li key={p}>{p}</li>)}</ul> : null}
        </details>
      ) : null}
    </div>
  );
}
