"use client";

import React from "react";
import { formatCost, formatScore, formatTokens } from "@/lib/format";
import type { SpanKind, TraceSpan } from "@/lib/traces";

// LP.15: the detail sections of the workflow loop spans (LP.9's `loop` and `loop_iteration`) and of
// `evaluate` spans. They read allowlisted attributes only: counts, indices, codes, verdicts, scores
// and judge usage. The API never puts a judge's reason or the judged value in them (the step's
// recorded output stays in the Output block, as for every step span).

/** Attributes each section shows, left out of the generic attribute list. */
export const LOOP_SHOWN: Partial<Record<SpanKind, readonly string[]>> = {
  loop: [
    "iterations_started", "iterations_completed", "iterations_failed", "iterations_omitted", "iterations", "exited",
  ],
  loop_iteration: ["index", "scope", "iteration_status", "until", "last_key", "failed_step_id"],
  evaluate: [
    "passed", "score", "evaluators", "evaluators_passed", "evaluators_failed", "evaluators_skipped", "results",
    "judge_requests", "judge_input_tokens", "judge_output_tokens", "judge_cost_usd", "scope",
  ],
};

function num(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function str(value: unknown): string | null {
  return typeof value === "string" && value !== "" ? value : null;
}

function Row({ label, children, testId }: { label: string; children: React.ReactNode; testId?: string }) {
  return (
    <div className="flex justify-between gap-4 py-0.5" data-testid={testId}>
      <dt className="shrink-0 text-muted-foreground">{label}</dt>
      <dd className="min-w-0 break-words text-right font-mono text-card-foreground">{children}</dd>
    </div>
  );
}

function Section({ title, testId, children, after }: { title: string; testId: string; children: React.ReactNode; after?: React.ReactNode }) {
  return (
    <section aria-label={title} data-testid={testId}>
      <h4 className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">{title}</h4>
      <dl className="mt-1 text-sm">{children}</dl>
      {after}
    </section>
  );
}

const EXITED_LABELS: Record<string, string> = { condition: "condition met", max_iterations: "max iterations reached" };

function LoopSection({ span }: { span: TraceSpan }) {
  const a = span.attributes;
  const omitted = num(a.iterations_omitted);
  return (
    <Section
      title="Loop"
      testId="span-loop"
      after={
        omitted ? (
          <p className="mt-1 text-xs text-muted-foreground" data-testid="span-loop-omitted">
            {omitted === 1 ? "1 iteration is" : `${omitted} iterations are`} not shown: the trace keeps the first ones of each loop.
          </p>
        ) : null
      }
    >
      {str(a.step_type) ? <Row label="Kind">{a.step_type as string}</Row> : null}
      <Row label="Iterations started" testId="span-loop-started">{formatTokens(num(a.iterations_started) ?? 0)}</Row>
      <Row label="Completed" testId="span-loop-completed">{formatTokens(num(a.iterations_completed) ?? 0)}</Row>
      <Row label="Failed" testId="span-loop-failed">{formatTokens(num(a.iterations_failed) ?? 0)}</Row>
      {str(a.exited) ? <Row label="Exited" testId="span-loop-exited">{EXITED_LABELS[a.exited as string] ?? (a.exited as string)}</Row> : null}
    </Section>
  );
}

const UNTIL_LABELS: Record<string, string> = { true: "met", false: "not met", unknown: "unknown (a value was missing)" };

function IterationSection({ span }: { span: TraceSpan }) {
  const a = span.attributes;
  const index = num(a.index);
  return (
    <Section title="Iteration" testId="span-loop-iteration">
      {index !== null ? <Row label="Iteration" testId="span-iteration-index">{index + 1}</Row> : null}
      {str(a.step_id) ? <Row label="Loop">{a.step_id as string}</Row> : null}
      {str(a.scope) ? <Row label="Scope">{a.scope as string}</Row> : null}
      {str(a.iteration_status) ? <Row label="Result" testId="span-iteration-status">{a.iteration_status as string}</Row> : null}
      {str(a.until) ? <Row label="Until" testId="span-iteration-until">{UNTIL_LABELS[a.until as string] ?? (a.until as string)}</Row> : null}
      {str(a.failed_step_id) ? <Row label="Failed step">{a.failed_step_id as string}</Row> : null}
    </Section>
  );
}

interface EvaluatorResult {
  name?: unknown;
  kind?: unknown;
  passed?: unknown;
  score?: unknown;
  skipped?: unknown;
  error_type?: unknown;
  error_code?: unknown;
}

function resultLabel(r: EvaluatorResult): string {
  if (r.skipped === true) return "skipped";
  if (str(r.error_code) || str(r.error_type)) return `error: ${str(r.error_code) ?? str(r.error_type)}`;
  return r.passed === true ? "passed" : r.passed === false ? "failed" : "-";
}

function EvaluateSection({ span }: { span: TraceSpan }) {
  const a = span.attributes;
  const results = (Array.isArray(a.results) ? a.results : []).filter((r): r is EvaluatorResult => !!r && typeof r === "object");
  const judges = num(a.judge_requests) ?? 0;
  const verdict = a.passed === true ? "passed" : a.passed === false ? "failed" : null;
  return (
    <Section
      title="Evaluation"
      testId="span-evaluate"
      after={
        results.length ? (
          <ul aria-label="Evaluator results" data-testid="span-evaluate-results" className="mt-1 space-y-0.5 text-sm">
            {results.map((r, i) => (
              <li key={i} data-testid="span-evaluate-result" data-passed={r.passed === true ? "true" : r.passed === false ? "false" : "none"} className="flex min-w-0 justify-between gap-4">
                <span className="min-w-0 break-words font-mono text-card-foreground">
                  {str(r.name) ?? `evaluator ${i + 1}`}
                  {str(r.kind) && r.kind !== r.name ? <span className="text-muted-foreground"> ({r.kind as string})</span> : null}
                </span>
                <span className="shrink-0 font-mono text-muted-foreground">
                  {resultLabel(r)}
                  {num(r.score) !== null ? `, ${formatScore(r.score)}` : ""}
                </span>
              </li>
            ))}
          </ul>
        ) : null
      }
    >
      {str(a.scope) ? <Row label="Scope" testId="span-evaluate-scope">{a.scope as string}</Row> : null}
      {verdict ? <Row label="Verdict" testId="span-evaluate-verdict">{verdict}</Row> : null}
      {num(a.score) !== null ? <Row label="Score" testId="span-evaluate-score">{formatScore(a.score)}</Row> : null}
      {num(a.evaluators) !== null ? (
        <Row label="Evaluators">
          {`${num(a.evaluators_passed) ?? 0} passed, ${num(a.evaluators_failed) ?? 0} failed${num(a.evaluators_skipped) ? `, ${a.evaluators_skipped as number} skipped` : ""} of ${a.evaluators as number}`}
        </Row>
      ) : null}
      <Row label="Judge requests" testId="span-evaluate-judges">{formatTokens(judges)}</Row>
      {judges > 0 ? (
        <>
          <Row label="Judge tokens" testId="span-evaluate-judge-tokens">
            {`${formatTokens(num(a.judge_input_tokens) ?? 0)} in, ${formatTokens(num(a.judge_output_tokens) ?? 0)} out`}
          </Row>
          <Row label="Judge cost" testId="span-evaluate-judge-cost">{formatCost(str(a.judge_cost_usd)) ?? "no price"}</Row>
        </>
      ) : null}
    </Section>
  );
}

/** The loop or evaluate section of a span, or null for other kinds. */
export function LoopSpanSection({ span }: { span: TraceSpan }) {
  switch (span.kind) {
    case "loop":
      return <LoopSection span={span} />;
    case "loop_iteration":
      return <IterationSection span={span} />;
    case "evaluate":
      return <EvaluateSection span={span} />;
    default:
      return null;
  }
}
