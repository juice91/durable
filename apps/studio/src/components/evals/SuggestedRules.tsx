"use client";

import React, { useCallback, useEffect, useId, useRef, useState } from "react";
import { Check, Sparkles, X } from "lucide-react";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { EVALUATOR_LABELS, evaluatorSummary, type EvalSuite, type EvalTargetRef } from "@/lib/evals";
import { ICON_STROKE_WIDTH } from "@/lib/icons";
import {
  addAsScorer,
  addToSuite,
  canBeScorer,
  defaultRulesSuiteName,
  listTargetSuites,
  suggestRules,
  type ScorerSuggestion,
  type ScorerSuggestions,
} from "@/lib/scorerSuggestions";
import { alertClass, cardClass, fieldClass, hintClass, sectionTitleClass } from "./evalsUi";
import { Select } from "@/components/ui/Select";

const NEW_SUITE = "__new__";
const SCOPE_LABELS = { scorer: "Scorer", evaluator: "Suite evaluator", both: "Scorer or suite" } as const;

type CardState = { added: string | null; dismissed: boolean };

/**
 * Suggested rules (Tests tab of an agent or workflow, Scorers section of the agent page): a "Suggest
 * rules" button asks the built-in suggester for rules read from the definition and recent runs. Each
 * card can be added as a reusable scorer (agents; attached at 100% sampling), appended to a suite as an
 * evaluator (an existing suite, or a new "<name> rules" suite), or dismissed. `onChanged` runs after
 * anything is created, so the host can reload its own lists.
 */
export function SuggestedRules({ target, onChanged, heading = "Suggested rules" }: { target: EvalTargetRef; onChanged?: () => void; heading?: string }) {
  const [result, setResult] = useState<ScorerSuggestions | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [cards, setCards] = useState<Record<number, CardState>>({});
  const [suites, setSuites] = useState<EvalSuite[]>([]);
  const request = useRef<AbortController | null>(null);

  useEffect(() => () => request.current?.abort(), []);

  const loadSuites = useCallback(async () => {
    try {
      setSuites(await listTargetSuites(target));
    } catch {
      setSuites([]); // the card then offers only a new suite
    }
  }, [target]);

  const suggest = async () => {
    request.current?.abort();
    const controller = new AbortController();
    request.current = controller;
    setLoading(true);
    setError(null);
    try {
      const [answer] = await Promise.all([suggestRules(target, { signal: controller.signal }), loadSuites()]);
      if (controller.signal.aborted) return;
      setResult(answer);
      setCards({});
    } catch (reason: unknown) {
      if (controller.signal.aborted) return;
      setError(reason instanceof Error && reason.message ? reason.message : "Suggestions could not be loaded");
    } finally {
      if (!controller.signal.aborted) setLoading(false);
    }
  };

  const mark = (index: number, patch: Partial<CardState>) => setCards((current) => ({ ...current, [index]: { ...(current[index] ?? { added: null, dismissed: false }), ...patch } }));
  const visible = (result?.suggestions ?? []).map((suggestion, index) => ({ suggestion, index })).filter(({ index }) => !cards[index]?.dismissed);

  return (
    <section aria-label="Suggested rules" data-testid="suggested-rules" className="min-w-0 space-y-3">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="min-w-0">
          <h2 className={sectionTitleClass}>{heading}</h2>
          <p className={hintClass}>Rules proposed from the {target.kind === "workflow" ? "workflow's steps" : "agent's instructions"} and its recent runs. Nothing is added until you click.</p>
        </div>
        <Button variant="secondary" size="sm" onClick={() => void suggest()} disabled={loading} data-testid="suggest-rules">
          <Sparkles strokeWidth={ICON_STROKE_WIDTH} aria-hidden="true" />
          {loading ? "Reading the definition…" : result ? "Suggest again" : "Suggest rules"}
        </Button>
      </div>
      {error ? <p role="alert" className={alertClass} data-testid="suggest-rules-error">{error}</p> : null}
      {result && visible.length === 0 && !error ? (
        <p className={hintClass} data-testid="suggest-rules-empty">{result.suggestions.length === 0 ? "No rules could be suggested yet. Run the agent a few times and try again." : "All suggestions are dismissed."}</p>
      ) : null}
      {visible.length > 0 ? (
        <ul className="grid min-w-0 gap-3 [grid-template-columns:repeat(auto-fill,minmax(min(100%,20rem),1fr))]" data-testid="suggested-rule-list">
          {visible.map(({ suggestion, index }) => (
            <SuggestionCard
              key={index}
              target={target}
              suggestion={suggestion}
              suites={suites}
              added={cards[index]?.added ?? null}
              onAdded={(label) => {
                mark(index, { added: label });
                onChanged?.();
                void loadSuites();
              }}
              onDismiss={() => mark(index, { dismissed: true })}
            />
          ))}
        </ul>
      ) : null}
      {result && result.dropped > 0 ? <p className={hintClass}>{result.dropped} {result.dropped === 1 ? "suggestion was" : "suggestions were"} left out because they were not valid rules.</p> : null}
    </section>
  );
}

function SuggestionCard({ target, suggestion, suites, added, onAdded, onDismiss }: {
  target: EvalTargetRef;
  suggestion: ScorerSuggestion;
  suites: EvalSuite[];
  added: string | null;
  onAdded: (label: string) => void;
  onDismiss: () => void;
}) {
  const id = useId();
  const defaultName = defaultRulesSuiteName(target);
  const [choosing, setChoosing] = useState(false);
  const [suiteId, setSuiteId] = useState(NEW_SUITE);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const asScorer = canBeScorer(target, suggestion);

  const run = async (work: () => Promise<string>, label: (name: string) => string) => {
    if (busy) return;
    setBusy(true);
    setError(null);
    try {
      onAdded(label(await work()));
      setChoosing(false);
    } catch (reason: unknown) {
      setError(reason instanceof Error && reason.message ? reason.message : "It could not be added");
    } finally {
      setBusy(false);
    }
  };

  const openChooser = () => {
    const existing = suites.find((s) => s.name === defaultName);
    setSuiteId(existing ? existing.id : NEW_SUITE);
    setChoosing(true);
  };

  return (
    <li className={`${cardClass} flex flex-col gap-2 p-3`} data-testid="suggested-rule" data-scope={suggestion.scope}>
      <div className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1">
        <h3 className="min-w-0 break-words text-label font-semibold text-foreground">{suggestion.title}</h3>
        <Badge variant="info" data-testid="suggested-rule-kind">{EVALUATOR_LABELS[suggestion.spec.kind] ?? suggestion.spec.kind}</Badge>
        <Badge variant="neutral">{SCOPE_LABELS[suggestion.scope]}</Badge>
      </div>
      <p className="break-words text-body text-foreground">{suggestion.rationale}</p>
      <p className="break-words rounded-md bg-muted px-2 py-1 font-mono text-caption text-foreground" data-testid="suggested-rule-spec">{evaluatorSummary(suggestion.spec)}</p>
      {suggestion.evidence ? <p className={`${hintClass} break-words`}>From: {suggestion.evidence}</p> : null}
      {error ? <p role="alert" className="text-caption text-destructive-text">{error}</p> : null}
      {added ? (
        <p className="flex items-center gap-1 text-label text-success-text" data-testid="suggested-rule-added">
          <Check strokeWidth={ICON_STROKE_WIDTH} className="size-4" aria-hidden="true" /> Added {added}
        </p>
      ) : choosing ? (
        <div className="flex flex-wrap items-end gap-2" role="group" aria-label={`Add “${suggestion.title}” to a suite`}>
          <div className="min-w-0 flex-1 space-y-1">
            <label htmlFor={`${id}-suite`} className="block text-caption text-muted-foreground">Suite</label>
            <Select id={`${id}-suite`} className={fieldClass} value={suiteId} onValueChange={(v) => setSuiteId(v)} data-testid="suggested-rule-suite">
              {suites.map((s) => <option key={s.id} value={s.id}>{s.name}</option>)}
              {suites.some((s) => s.name === defaultName) ? null : <option value={NEW_SUITE}>New suite “{defaultName}”</option>}
            </Select>
          </div>
          <Button size="sm" variant="primary" disabled={busy} onClick={() => void run(() => addToSuite(target, suggestion, suiteId === NEW_SUITE ? null : suites.find((s) => s.id === suiteId) ?? null), (name) => `to suite “${name}”`)} data-testid="suggested-rule-confirm">
            {busy ? "Adding…" : "Add"}
          </Button>
          <Button size="sm" variant="ghost" disabled={busy} onClick={() => setChoosing(false)}>Cancel</Button>
        </div>
      ) : (
        <div className="flex flex-wrap items-center gap-2">
          {asScorer ? (
            <Button size="sm" variant="primary" disabled={busy} onClick={() => void run(() => addAsScorer(target.id, suggestion), (name) => `as scorer “${name}”`)} data-testid="suggested-rule-add-scorer">
              {busy ? "Adding…" : "Add as scorer"}
            </Button>
          ) : null}
          <Button size="sm" variant="secondary" disabled={busy} onClick={openChooser} data-testid="suggested-rule-add-suite">Add to suite</Button>
          <Button size="sm" variant="ghost" onClick={onDismiss} aria-label={`Dismiss “${suggestion.title}”`} data-testid="suggested-rule-dismiss">
            <X strokeWidth={ICON_STROKE_WIDTH} aria-hidden="true" />
            Dismiss
          </Button>
        </div>
      )}
    </li>
  );
}
