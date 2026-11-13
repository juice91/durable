"use client";

import React, { useRef, useState } from "react";
import { ProcessorConfigForm } from "@/components/processors/ProcessorConfigForm";
import { ApiError } from "@/lib/api";
import {
  defaultConfig,
  formatMatchCounts,
  isModerationRefusal,
  MODERATION_TEST_MESSAGE,
  processors as processorsApi,
  type ProcessorCatalog,
  type ProcessorTestResult,
  type ProcessorType,
} from "@/lib/processors";

const inputClass = "w-full rounded-md border border-input bg-background px-3 py-2 text-sm text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring";
const secondaryButton = "rounded-md border border-border px-3 py-1.5 text-sm text-foreground hover:bg-accent disabled:cursor-not-allowed disabled:opacity-50";

interface Step {
  key: number;
  type: ProcessorType;
  config: Record<string, unknown>;
}

function actionClass(action: string): string {
  if (action === "block") return "bg-destructive/10 text-destructive-text";
  if (action === "allow") return "bg-muted text-muted-foreground";
  return "bg-primary/10 text-primary";
}

/**
 * Runs deterministic processors, in order, on a sample in this request only: nothing is stored.
 * Moderation calls a model and is tested in the playground instead.
 */
export function ProcessorTestConsole({ catalog, initialType }: { catalog: ProcessorCatalog; initialType?: ProcessorType }) {
  const testable = catalog.items.filter((item) => item.deterministic);
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

  if (testable.length === 0) {
    return <p className="text-sm text-muted-foreground">No processor type can run in the test console.</p>;
  }

  const changed = () => {
    inputVersion.current += 1;
    setResult(null);
    setError(null);
  };

  const update = (key: number, change: Partial<Step>) => {
    changed();
    setSteps((current) => current.map((step) => (step.key === key ? { ...step, ...change } : step)));
  };

  const addStep = () => {
    changed();
    const type = testable[0].id;
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

  return (
    <form onSubmit={(event) => void run(event)} aria-label="Processor test console" className="space-y-4">
      <ol className="space-y-3">
        {steps.map((step, index) => (
          <li key={step.key} data-testid="console-step" className="rounded-md border border-border p-3">
            <div className="mb-3 flex flex-wrap items-end gap-2">
              <div className="min-w-48 flex-1">
                <label htmlFor={`console-type-${step.key}`} className="block text-sm font-medium text-foreground">Processor {index + 1}</label>
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
                  {untestable.map((item) => <option key={item.id} value={item.id} disabled>{item.title} (test in the playground)</option>)}
                </select>
              </div>
              <button type="button" className={secondaryButton} disabled={index === 0} onClick={() => move(index, -1)} aria-label={`Move processor ${index + 1} up`}>Up</button>
              <button type="button" className={secondaryButton} disabled={index === steps.length - 1} onClick={() => move(index, 1)} aria-label={`Move processor ${index + 1} down`}>Down</button>
              <button type="button" className={secondaryButton} disabled={steps.length === 1} onClick={() => remove(step.key)} aria-label={`Remove processor ${index + 1}`}>Remove</button>
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
      <div className="flex flex-wrap items-center gap-3">
        <button type="button" className={secondaryButton} disabled={steps.length >= maxSteps} onClick={addStep}>Add processor</button>
        <span className="text-xs text-muted-foreground">Processors run in order, each on the previous one&apos;s output, and stop at the first block (at most {maxSteps}).</span>
      </div>
      {untestable.length > 0 ? (
        <p className="text-xs text-muted-foreground">{untestable.map((item) => item.title).join(", ")}: {MODERATION_TEST_MESSAGE}</p>
      ) : null}

      <div className="space-y-1">
        <label htmlFor="console-sample" className="block text-sm font-medium text-foreground">Sample text</label>
        <textarea id="console-sample" rows={5} className={inputClass} value={text} onChange={(event) => { changed(); setText(event.target.value); }} placeholder="Text to run the processors on" />
        <p className={`text-xs ${sampleBytes > maxBytes ? "text-destructive-text" : "text-muted-foreground"}`}>
          {sampleBytes.toLocaleString()} of {maxBytes.toLocaleString()} bytes. The sample is processed for this request only and is not stored.
        </p>
      </div>

      <button type="submit" disabled={busy || steps.length === 0} className="rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground disabled:cursor-not-allowed disabled:opacity-50">
        {busy ? "Running…" : "Run test"}
      </button>

      {error ? <p role="alert" data-testid="console-error" className="rounded-md border border-destructive/30 bg-destructive/10 p-3 text-sm text-destructive-text">{error}</p> : null}

      {result ? (
        <section data-testid="console-result" aria-label="Test result" className="space-y-3 rounded-md border border-border p-3">
          <p className={`text-sm font-medium ${result.blocked ? "text-destructive-text" : "text-foreground"}`}>
            {result.blocked ? "Blocked: the text would not be passed on." : "Passed."}
          </p>
          <ol className="space-y-1 text-sm">
            {result.results.map((outcome) => (
              <li key={outcome.index} data-testid="console-outcome" className="flex flex-wrap items-center gap-2">
                <span className="text-muted-foreground">{outcome.index + 1}.</span>
                <span className="font-mono text-xs">{outcome.processor_type}</span>
                <span className={`rounded-full px-2 py-0.5 text-xs ${actionClass(outcome.action)}`}>{outcome.action}</span>
                <span className="text-xs text-muted-foreground">{formatMatchCounts(outcome.match_counts)}</span>
                {outcome.reason ? <span className="text-xs text-muted-foreground">({outcome.reason})</span> : null}
              </li>
            ))}
          </ol>
          {result.text !== null ? (
            <div>
              <h4 className="mb-1 text-xs font-medium uppercase tracking-wide text-muted-foreground">Processed text</h4>
              <pre data-testid="console-output" className="max-h-64 overflow-auto whitespace-pre-wrap break-words rounded-md bg-muted p-2 text-xs text-foreground">{result.text}</pre>
            </div>
          ) : null}
        </section>
      ) : null}
    </form>
  );
}
