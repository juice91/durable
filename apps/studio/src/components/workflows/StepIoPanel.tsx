"use client";

import React from "react";
import { formatValue } from "@/components/runs/EventTimeline";
import { NODE_STATE_CLASSES, NODE_STATE_LABELS } from "./WorkflowGraph";
import { formatDateTime } from "@/components/approvals/ApprovalCard";
import type { IterationDetail, StepDetail } from "@/lib/workflowRunState";
import type { WorkflowStep } from "@/lib/workflows";
import { TraceLink } from "@/components/traces/TraceLink";
import { formatScore } from "@/lib/format";
import { CopyableText } from "@/components/ui/CopyableText";
import { CopyButton } from "@/components/ui/CopyButton";
import { BranchCases, branchCasesText, branchTaken } from "./BranchCases";
import { evaluationView, refineAttempts, refineEvaluateStep, type EvaluationView, type RefineAttempt } from "./refineAttempts";

/** What a copy of a value gives: a string as it is, anything else as raw (indented) JSON. */
function rawText(value: unknown): string {
  if (typeof value === "string") return value;
  try {
    return JSON.stringify(value, null, 2) ?? String(value);
  } catch {
    return String(value);
  }
}

function Block({ label, value, testId, format }: { label: string; value: unknown; testId: string; format?: "json" | "text" }) {
  return (
    <div>
      <h4 className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">{label}</h4>
      <CopyableText label={label.toLowerCase()} copyText={rawText(value)} className="mt-1">
        <pre data-testid={testId} data-output-format={format} className="max-h-64 overflow-auto whitespace-pre-wrap break-words rounded-md bg-muted p-2 pr-9 font-mono text-xs text-foreground">{formatValue(value)}</pre>
      </CopyableText>
    </div>
  );
}

/** A clipped scroll region that a keyboard can reach and scroll (axe scrollable-region-focusable):
 * model text (reasons, drafts) can run to thousands of characters. */
function Scroller({ label, className, testId, children }: { label: string; className: string; testId?: string; children: React.ReactNode }) {
  return (
    <div role="region" aria-label={label} tabIndex={0} data-testid={testId} className={`${className} min-w-0 overflow-auto rounded-sm outline-none focus-visible:ring-2 focus-visible:ring-ring`}>
      {children}
    </div>
  );
}

/** An agent step's output is an object when its agent has structured output (AC.9), else its text. */
export function isStructuredAgentOutput(type: string | null | undefined, output: unknown): boolean {
  return type === "agent" && output !== null && typeof output === "object";
}

function Verdict({ passed, testId }: { passed: boolean | null; testId?: string }) {
  if (passed === null) return <span data-testid={testId} data-passed="none" className="inline-block rounded border border-border px-1.5 text-badge text-muted-foreground">No verdict</span>;
  return (
    <span data-testid={testId} data-passed={passed ? "true" : "false"} className={`inline-block rounded border px-1.5 text-badge ${NODE_STATE_CLASSES[passed ? "completed" : "failed"]}`}>
      {passed ? "Passed" : "Failed"}
    </span>
  );
}

function resultLabel(r: EvaluationView["results"][number]): string {
  if (r.skipped) return "Skipped";
  if (r.errorCode) return `Error (${r.errorCode})`;
  return r.passed === true ? "Passed" : r.passed === false ? "Failed" : "-";
}

/** LP.15: an evaluate step's verdict, score, reason and per-evaluator results (plain text, never Markdown). */
function EvaluationSection({ view }: { view: EvaluationView }) {
  return (
    <div data-testid="step-io-evaluation" className="min-w-0 space-y-2">
      <h4 className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">Evaluation</h4>
      <p className="flex flex-wrap items-center gap-2 text-sm">
        <Verdict passed={view.passed} testId="step-io-evaluation-verdict" />
        <span className="text-muted-foreground">Score <span data-testid="step-io-evaluation-score" className="font-mono tabular-nums text-foreground">{formatScore(view.score)}</span></span>
      </p>
      {view.reason ? (
        <Scroller label="Evaluation reason" className="max-h-40">
          <p data-testid="step-io-evaluation-reason" className="whitespace-pre-wrap break-words text-sm text-foreground">{view.reason}</p>
        </Scroller>
      ) : null}
      {view.results.length > 0 ? (
        <ul aria-label="Evaluator results" className="min-w-0 divide-y divide-border rounded-md border border-border">
          {view.results.map((r, i) => (
            <li key={`${r.name}-${i}`} data-testid="step-io-evaluator-result" data-passed={r.passed === null ? "none" : String(r.passed)} data-skipped={r.skipped || undefined} className="min-w-0 space-y-0.5 p-2 text-caption">
              <div className="flex min-w-0 flex-wrap items-baseline justify-between gap-x-2">
                <span className="min-w-0 break-words font-mono text-foreground">
                  {r.name}
                  {r.kind && r.kind !== r.name ? <span className="text-muted-foreground"> ({r.kind})</span> : null}
                </span>
                <span className="shrink-0 font-mono tabular-nums text-muted-foreground">{resultLabel(r)}{r.score !== null ? `, ${formatScore(r.score)}` : ""}</span>
              </div>
              {r.reason ? (
                <Scroller label={`Reason of evaluator ${i + 1} (${r.name})`} className="max-h-24">
                  <p data-testid="step-io-evaluator-reason" className="whitespace-pre-wrap break-words text-muted-foreground">{r.reason}</p>
                </Scroller>
              ) : null}
            </li>
          ))}
        </ul>
      ) : null}
    </div>
  );
}

function draftText(draft: unknown): string | null {
  if (draft === undefined) return null;
  return typeof draft === "string" ? draft : formatValue(draft);
}

/** LP.15 (loops D15): a refine loop's attempts, one per iteration: the draft, score and reason. */
export function RefineAttempts({ attempts, evaluateStepId }: { attempts: readonly RefineAttempt[]; evaluateStepId: string }) {
  return (
    <div data-testid="step-refine-attempts" className="min-w-0">
      <h4 className="text-caption font-semibold uppercase tracking-wide text-muted-foreground">Attempts ({attempts.length})</h4>
      <p className="text-caption text-muted-foreground">Judged by <span className="font-mono">{evaluateStepId}</span>.</p>
      <Scroller label="Refine attempts" className="mt-1 max-h-96" testId="step-refine-attempts-scroll">
      <ol aria-label="Refine attempts" className="min-w-0 space-y-2">
        {attempts.map((a) => {
          const draft = draftText(a.draft);
          const ev = a.evaluation;
          return (
            <li key={a.scope} data-testid="step-refine-attempt" data-index={a.index} data-passed={ev && ev.passed !== null ? String(ev.passed) : "none"} className="min-w-0 space-y-1 rounded-md border border-border bg-surface p-2">
              <div className="flex min-w-0 flex-wrap items-center gap-2 text-caption">
                <span className="font-medium text-foreground">Attempt {a.index + 1}</span>
                {ev ? <Verdict passed={ev.passed} /> : <span className={`rounded-sm border px-1 text-badge ${NODE_STATE_CLASSES[a.iterationState]}`}>{a.errorCode ?? NODE_STATE_LABELS[a.iterationState]}</span>}
                {ev ? <span className="text-muted-foreground">Score <span data-testid="step-refine-attempt-score" className="font-mono tabular-nums text-foreground">{formatScore(ev.score)}</span></span> : null}
              </div>
              {draft !== null ? (
                <Scroller label={`Attempt ${a.index + 1} draft`} className="max-h-40">
                  <pre data-testid="step-refine-attempt-draft" className="whitespace-pre-wrap break-words rounded-sm bg-muted p-1.5 font-mono text-caption text-foreground">{draft}</pre>
                </Scroller>
              ) : null}
              {ev?.reason ? <p data-testid="step-refine-attempt-reason" className="whitespace-pre-wrap break-words text-caption text-muted-foreground">{ev.reason}</p> : null}
            </li>
          );
        })}
      </ol>
      </Scroller>
    </div>
  );
}

const DECISION_LABELS = { approved: "Approved", rejected: "Rejected", timed_out: "Timed out" } as const;

export interface StepIoPanelProps {
  step: WorkflowStep | null;
  detail: StepDetail;
  onClose: () => void;
  className?: string;
  /** A loop body step: which iteration this detail is from ("Iteration 3 of each"). */
  iterationLabel?: string | null;
  /** Extra sections after the step's own (a loop step's iteration table, LP.12). */
  children?: React.ReactNode;
  /** A loop step's iterations (its pager's): a refine loop lists its attempts from them (LP.15). */
  iterations?: readonly IterationDetail[] | null;
}

/** Input, output or error of one step, from its step_* events (a body step: of one iteration). */
export function StepIoPanel({ step, detail, onClose, className, iterationLabel, children, iterations }: StepIoPanelProps) {
  const type = step?.type ?? detail.stepType;
  const evaluation = type === "evaluate" && detail.hasOutput ? evaluationView(detail.output) : null;
  const attempts = refineAttempts(step, iterations);
  const judgedBy = attempts.length > 0 ? refineEvaluateStep(step) : null;
  return (
    <section data-testid="step-io-panel" data-step-id={detail.stepId} aria-label={`Step ${detail.stepId} input and output`} className={className ?? "min-w-0 space-y-3 rounded-lg border border-border bg-surface-raised p-4"}>
      <div className="flex items-center justify-between gap-2">
        <h3 className="font-semibold text-card-foreground">
          <span data-testid="step-io-step-id" className="select-text font-mono">{detail.stepId}</span>
          <CopyButton text={detail.stepId} label="step id" className="ml-1 align-middle" />
          {type ? <span className="ml-2 text-xs font-normal uppercase text-muted-foreground">{type}</span> : null}
        </h3>
        <button type="button" onClick={onClose} aria-label="Close step details" className="rounded-sm text-caption text-muted-foreground outline-none hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring">Close</button>
      </div>
      <p>
        <span data-testid="step-io-state" className={`inline-block rounded border px-2 py-0.5 text-xs ${NODE_STATE_CLASSES[detail.state]}`}>{NODE_STATE_LABELS[detail.state]}</span>
        {detail.errorKept ? (
          <span data-testid="step-io-error-kept" title="The tool reported an error; the step kept it as its output and the run went on." className="ml-2 inline-block rounded border border-warning/40 bg-warning/10 px-2 py-0.5 text-xs text-warning-text">
            error kept: <code className="font-mono">{detail.errorKept}</code>
          </span>
        ) : null}
        {detail.startedAt ? <span className="ml-2 text-xs text-muted-foreground">started {formatDateTime(detail.startedAt)}</span> : null}
      </p>
      {iterationLabel ? <p data-testid="step-io-iteration" className="text-caption text-muted-foreground">{iterationLabel}</p> : null}
      {detail.childRunId ? (
        <p className="text-sm">
          {/* The child run's trace: in place, as the trace Sheet, on a page that hosts one (SL.12). */}
          <TraceLink data-testid="step-child-run-link" runId={detail.childRunId} tab="events" className="text-info-text underline-offset-4 hover:underline">
            Open agent run {detail.childRunId.slice(0, 8)}
          </TraceLink>
        </p>
      ) : null}
      {detail.toolId ? <p className="text-xs text-muted-foreground">Tool <span className="font-mono">{detail.toolId}</span></p> : null}
      {type === "approval" ? (
        <div className="space-y-1 text-sm">
          <p className="whitespace-pre-wrap text-foreground">{detail.prompt ?? (step?.type === "approval" ? step.prompt : "")}</p>
          {detail.deadline ? <p className="text-xs text-muted-foreground">Deadline {formatDateTime(detail.deadline)}</p> : null}
          {detail.decision ? (
            <div data-testid="step-decision" data-decision={detail.decision.kind} className="rounded-md border border-border bg-muted/40 p-2">
              <p className="font-medium text-foreground">{DECISION_LABELS[detail.decision.kind]}{detail.decision.decidedBy ? <> by <span data-testid="step-decided-by" className="font-mono">{detail.decision.decidedBy}</span></> : null}</p>
              {detail.decision.comment ? <p data-testid="step-decision-comment" className="mt-1 whitespace-pre-wrap text-muted-foreground">{detail.decision.comment}</p> : null}
            </div>
          ) : detail.state === "waiting" ? <p className="text-xs text-muted-foreground">Decide in the approval card (Needs input).</p> : null}
        </div>
      ) : null}
      {step?.type === "branch" ? (
        <div>
          <h4 className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">Cases</h4>
          <CopyableText label="cases" copyText={branchCasesText(step)} data-testid="step-io-cases" className="mt-1">
            <BranchCases step={step} taken={branchTaken(detail.output)} warnings={detail.warnings} />
          </CopyableText>
        </div>
      ) : null}
      {detail.input !== undefined ? <Block label="Input" value={detail.input} testId="step-io-input" /> : null}
      {detail.hasOutput ? (
        type === "parallel" && detail.output === null ? (
          <div>
            <h4 className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">Output</h4>
            <p data-testid="step-io-output" className="mt-1 text-sm text-muted-foreground">No output (parallel step).</p>
          </div>
        ) : type === "agent" ? (
          // AC.9: a structured agent's validated object, shown as escaped JSON text (kept so: the
          // chat's tree view renders strings as Markdown); a text agent's answer stays text.
          isStructuredAgentOutput(type, detail.output)
            ? <Block label="Structured output" value={detail.output} testId="step-io-output" format="json" />
            : <Block label="Output" value={detail.output} testId="step-io-output" format="text" />
        ) : evaluation ? (
          <>
            <EvaluationSection view={evaluation} />
            <details className="text-sm">
              <summary className="cursor-pointer rounded-sm text-caption text-muted-foreground outline-none focus-visible:ring-2 focus-visible:ring-ring">Raw output</summary>
              <div className="mt-1"><Block label="Output" value={detail.output} testId="step-io-output" /></div>
            </details>
          </>
        ) : <Block label="Output" value={detail.output} testId="step-io-output" />
      ) : null}
      {detail.unresolvedRefs && detail.unresolvedRefs.length > 0 ? (
        <div role="note" data-testid="step-io-unresolved-refs" className="rounded-md border border-warning/40 bg-warning/10 p-2 text-sm text-warning-text">
          <p className="font-medium">
            {detail.unresolvedRefs.length === 1 ? "A reference resolved to nothing" : `${detail.unresolvedRefs.length} references resolved to nothing`}
            {type === "branch" ? "; the branch took its default." : "."}
          </p>
          <ul className="mt-1 space-y-0.5">
            {detail.unresolvedRefs.map((ref) => (
              <li key={ref} className="break-all font-mono text-xs">{ref}</li>
            ))}
          </ul>
        </div>
      ) : null}
      {detail.error || detail.errorCode ? (
        <div role="alert" data-testid="step-io-error" className="rounded-md border border-destructive/30 bg-destructive/10 p-2 text-sm text-destructive-text">
          <CopyableText label="error" copyText={[detail.error, detail.errorCode ? `Error code: ${detail.errorCode}` : null].filter(Boolean).join("\n")} className="pr-8">
            {detail.error ? <p className="whitespace-pre-wrap">{detail.error}</p> : null}
            {detail.errorCode ? <p className="mt-1 text-xs">Error code: <code className="font-mono">{detail.errorCode}</code></p> : null}
          </CopyableText>
        </div>
      ) : null}
      {!detail.started && detail.state === "pending" ? <p className="text-sm text-muted-foreground">This step has not started.</p> : null}
      {detail.state === "skipped" ? (
        <p data-testid="step-io-skip-reason" data-skip-reason={detail.skipReason ?? undefined} className="text-sm text-muted-foreground">
          {detail.skipReason === "run_ended"
            ? "Not reached: the run ended before this step."
            : detail.skipReason === "iteration_ended"
              ? "Not run in this iteration."
              : "Not reached: this branch arm was not taken."}
        </p>
      ) : null}
      {attempts.length > 0 && judgedBy ? <RefineAttempts attempts={attempts} evaluateStepId={judgedBy.id} /> : null}
      {children}
    </section>
  );
}
