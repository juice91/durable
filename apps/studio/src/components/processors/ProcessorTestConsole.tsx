"use client";

import React, { useRef, useState } from "react";
import { ProcessorConfigForm } from "@/components/processors/ProcessorConfigForm";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { ApiError } from "@/lib/api";
import {
  defaultConfig,
  formatMatchCounts,
  isModerationRefusal,
  MODERATION_TEST_MESSAGE,
  PROCESSOR_PHASE_LABELS,
  PROCESSOR_PHASES,
  processors as processorsApi,
  type ProcessorPhase,
  type ProcessorCatalog,
  type ProcessorTestResult,
  type ProcessorType,
} from "@/lib/processors";

const inputClass = "w-full min-w-0 rounded-md border border-input bg-surface px-3 py-2 text-label text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring";

/**
 * The test's one-line verdict: "Blocked: …", or "Passed." plus what changed ("Redacted 2 matches.",
 * "Truncated."). Counts only, never matched text.
 */
export function consoleVerdict(result: ProcessorTestResult): string {
  if (result.blocked) return "Blocked: the text would not be passed on.";
  const redacted = result.results
    .filter((outcome) => outcome.action === "redact")
    .reduce((sum, outcome) => sum + Object.values(outcome.match_counts ?? {}).reduce((n, v) => n + (typeof v === "number" && v > 0 ? v : 0), 0), 0);
  const parts = ["Passed."];
  if (redacted > 0) parts.push(`Redacted ${redacted} ${redacted === 1 ? "match" : "matches"}.`);
  if (result.results.some((outcome) => outcome.action === "truncate")) parts.push("Truncated.");
  return parts.join(" ");
}

interface Step {
  key: number;
  type: ProcessorType;
  config: Record<string, unknown>;
}

function actionVariant(action: string): "destructive" | "neutral" | "info" {
  if (action === "block") return "destructive";
  if (action === "allow") return "neutral";
  return "info";
}

/**
 * Runs deterministic processors, in order, on a sample in this request only: nothing is stored.
 * Moderation calls a model and is tested in an agent's chat instead. The form sits in a 22rem
 * column with the result (outcomes, processed text, response JSON) beside it; stacked below 1024 px.
 */
export function ProcessorTestConsole({ catalog, initialType }: { catalog: ProcessorCatalog; initialType?: ProcessorType }) {
  // The phase only narrows which types are offered (the test runs the same deterministic chain in
  // every phase); a type that does not run in the phase cannot be chosen.
  const [phase, setPhase] = useState<ProcessorPhase>("input");
  const testable = catalog.items.filter((item) => item.deterministic && item.phases.includes(phase));
  const untestable = catalog.items.filter((item) => !item.deterministic);
  const schemaOf = (type: ProcessorType) => catalog.items.find((item) => item.id === type)?.config_schema ?? {};
  const firstType = initialType && testable.some((item) => item.id === initialType) ? initialType : testable[0]?.id;

  const [nextKey, setNextKey] = useState(1);
  const [steps, setSteps] = useState<Step[]>(firstType ? [{ key: 0, type: firstType, config: defaultConfig(schemaOf(firstType)) }] : []);
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<ProcessorTestResult | null>(null);
  // Bumped on every change to the steps or the sample: an answer for older input is dropped.
  const inputVersion = useRef(0);

  const maxSteps = catalog.limits.max_processors_per_phase;
  const maxBytes = catalog.limits.max_test_sample_bytes;
  const sampleBytes = new TextEncoder().encode(text).length;

  if (!catalog.items.some((item) => item.deterministic)) {
    return <p className="text-sm text-muted-foreground">No processor type can run in the test console.</p>;
  }

  const changed = () => {
    inputVersion.current += 1;
    setResult(null);
    setError(null);
  };

  const changePhase = (next: ProcessorPhase) => {
    changed();
    setPhase(next);
    const allowed = catalog.items.filter((item) => item.deterministic && item.phases.includes(next));
    setSteps((current) =>
      allowed.length === 0
        ? current
        : current.map((step) => (allowed.some((item) => item.id === step.type) ? step : { ...step, type: allowed[0].id, config: defaultConfig(schemaOf(allowed[0].id)) })),
    );
  };

  const update = (key: number, change: Partial<Step>) => {
    changed();
    setSteps((current) => current.map((step) => (step.key === key ? { ...step, ...change } : step)));
  };

  const addStep = () => {
    changed();
    const type = (testable[0] ?? catalog.items.find((item) => item.deterministic)!).id;
    setSteps((current) => [...current, { key: nextKey, type, config: defaultConfig(schemaOf(type)) }]);
    setNextKey((key) => key + 1);
  };

  const move = (index: number, delta: number) => {
    changed();
    setSteps((current) => {
      const next = [...current];
      const [step] = next.splice(index, 1);
      next.splice(index + delta, 0, step);
      return next;
    });
  };

  const remove = (key: number) => {
    changed();
    setSteps((current) => current.filter((s) => s.key !== key));
  };

  const run = async (event: React.FormEvent) => {
    event.preventDefault();
    if (steps.length === 0 || busy) return;
    if (sampleBytes > maxBytes) {
      setError(`The sample is larger than ${Math.round(maxBytes / 1024)} KiB.`);
      return;
    }
    const mine = inputVersion.current;
    setBusy(true);
    setError(null);
    setResult(null);
    try {
      const answer = await processorsApi.test(steps.map(({ type, config }) => ({ type, config })), text);
      if (mine === inputVersion.current) setResult(answer);
    } catch (reason: unknown) {
      if (mine !== inputVersion.current) return;
      const message = reason instanceof Error ? reason.message : "The test failed";
      setError(reason instanceof ApiError && isModerationRefusal(message) ? MODERATION_TEST_MESSAGE : message);
    } finally {
      setBusy(false);
    }
  };

  const resultPanel = result ? (
    <section data-testid="console-result" aria-label="Test result" className="min-w-0 space-y-3 rounded-lg border border-border bg-surface p-3">
      <p data-testid="console-verdict" className={`text-body font-medium ${result.blocked ? "text-destructive-text" : "text-foreground"}`}>
        {consoleVerdict(result)}
      </p>
      <ol className="space-y-1 text-label">
        {result.results.map((outcome) => (
          <li key={outcome.index} data-testid="console-outcome" className="flex min-w-0 flex-wrap items-center gap-2">
            <span className="text-muted-foreground">{outcome.index + 1}.</span>
            <span className="font-mono text-caption">{outcome.processor_type}</span>
            <Badge variant={actionVariant(outcome.action)}>{outcome.action}</Badge>
            <span className="text-caption text-muted-foreground">{formatMatchCounts(outcome.match_counts)}</span>
            {outcome.reason ? <span className="text-caption text-muted-foreground">({outcome.reason})</span> : null}
          </li>
        ))}
      </ol>
      {result.text !== null ? (
        <div className="min-w-0">
          <h4 className="mb-1 text-caption font-medium uppercase tracking-wide text-muted-foreground">Processed text</h4>
          <pre data-testid="console-output" className="max-h-64 overflow-auto whitespace-pre-wrap break-words rounded-md bg-muted p-2 font-mono text-caption text-foreground">{result.text}</pre>
        </div>
      ) : null}
      <details className="min-w-0">
        <summary className="cursor-pointer text-caption text-muted-foreground">Response JSON</summary>
        <pre data-testid="console-json" className="mt-1 max-h-80 overflow-auto rounded-md bg-muted p-2 font-mono text-caption text-foreground">{JSON.stringify(result, null, 2)}</pre>
      </details>
    </section>
  ) : (
    <div data-testid="console-result-empty" className="flex min-h-32 min-w-0 items-center justify-center rounded-lg border border-dashed border-border p-6 text-center text-label text-muted-foreground">
      {busy ? "Running…" : "Run a test to see each processor's outcome and the processed text."}
    </div>
  );

  return (
    <div data-testid="console-layout" className="grid min-w-0 gap-4 lg:grid-cols-[22rem_minmax(0,1fr)]">
      <form onSubmit={(event) => void run(event)} aria-label="Processor test console" className="min-w-0 space-y-4">
        <div className="min-w-0">
          <label htmlFor="console-phase" className="block text-label font-medium text-foreground">Phase</label>
          <select id="console-phase" className={inputClass} value={phase} onChange={(event) => changePhase(event.target.value as ProcessorPhase)}>
            {PROCESSOR_PHASES.map((item) => <option key={item} value={item}>{PROCESSOR_PHASE_LABELS[item]}</option>)}
          </select>
          <p className="mt-1 text-caption text-muted-foreground">Only the types that run in this phase are offered. The test runs the same checks in every phase.</p>
        </div>
        <ol className="space-y-3">
          {steps.map((step, index) => (
            <li key={step.key} data-testid="console-step" className="min-w-0 rounded-lg border border-border bg-surface p-3">
              <div className="mb-3 space-y-2">
                <div className="min-w-0">
                  <label htmlFor={`console-type-${step.key}`} className="block text-label font-medium text-foreground">Processor {index + 1}</label>
                  <select
                    id={`console-type-${step.key}`}
                    className={inputClass}
                    value={step.type}
                    onChange={(event) => {
                      const type = event.target.value as ProcessorType;
                      update(step.key, { type, config: defaultConfig(schemaOf(type)) });
                    }}
                  >
                    {testable.map((item) => <option key={item.id} value={item.id}>{item.title}</option>)}
                    {untestable.map((item) => <option key={item.id} value={item.id} disabled>{item.title} (test in the chat)</option>)}
                  </select>
                </div>
                <div className="flex flex-wrap gap-1">
                  <Button size="sm" disabled={index === 0} onClick={() => move(index, -1)} aria-label={`Move processor ${index + 1} up`}>Up</Button>
                  <Button size="sm" disabled={index === steps.length - 1} onClick={() => move(index, 1)} aria-label={`Move processor ${index + 1} down`}>Down</Button>
                  <Button size="sm" disabled={steps.length === 1} onClick={() => remove(step.key)} aria-label={`Remove processor ${index + 1}`}>Remove</Button>
                </div>
              </div>
              <ProcessorConfigForm
                key={step.type}
                idPrefix={`console-${step.key}`}
                schema={schemaOf(step.type)}
                value={step.config}
                onChange={(config) => update(step.key, { config })}
              />
            </li>
          ))}
        </ol>
        <div className="space-y-1">
          <Button disabled={steps.length >= maxSteps} onClick={addStep}>Add processor</Button>
          <p className="text-caption text-muted-foreground">Processors run in order, each on the previous one&apos;s output, and stop at the first block (at most {maxSteps}).</p>
        </div>
        {untestable.length > 0 ? (
          <p className="text-caption text-muted-foreground">{untestable.map((item) => item.title).join(", ")}: {MODERATION_TEST_MESSAGE}</p>
        ) : null}

        <div className="space-y-1">
          <label htmlFor="console-sample" className="block text-label font-medium text-foreground">Sample text</label>
          <textarea id="console-sample" rows={5} className={inputClass} value={text} onChange={(event) => { changed(); setText(event.target.value); }} placeholder="Text to run the processors on" />
          <p className={`text-caption ${sampleBytes > maxBytes ? "text-destructive-text" : "text-muted-foreground"}`}>
            {sampleBytes.toLocaleString()} of {maxBytes.toLocaleString()} bytes. The sample is processed for this request only and is not stored.
          </p>
        </div>

        <Button type="submit" variant="primary" disabled={busy || steps.length === 0}>
          {busy ? "Running…" : "Run test"}
        </Button>

        {error ? <p role="alert" data-testid="console-error" className="rounded-md border border-destructive/30 bg-destructive/10 p-3 text-label text-destructive-text">{error}</p> : null}
      </form>
      <div className="min-w-0">
        {/* Only the one-line verdict is announced; the panel and "Running…" are not. */}
        <p role="status" className="sr-only" data-testid="console-status">{result ? consoleVerdict(result) : ""}</p>
        {resultPanel}
      </div>
    </div>
  );
}
