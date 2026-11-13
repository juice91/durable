"use client";

import React from "react";
import Link from "next/link";
import { formatValue } from "@/components/runs/EventTimeline";
import { NODE_STATE_CLASSES, NODE_STATE_LABELS } from "./WorkflowGraph";
import { formatDateTime } from "./ApprovalCard";
import type { StepDetail } from "@/lib/workflowRunState";
import type { WorkflowStep } from "@/lib/workflows";

function Block({ label, value, testId }: { label: string; value: unknown; testId: string }) {
  return (
    <div>
      <h4 className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">{label}</h4>
      <pre data-testid={testId} className="mt-1 max-h-64 overflow-auto whitespace-pre-wrap break-words rounded-md bg-muted p-2 font-mono text-xs text-foreground">{formatValue(value)}</pre>
    </div>
  );
}

const DECISION_LABELS = { approved: "Approved", rejected: "Rejected", timed_out: "Timed out" } as const;

/** Input, output or error of one step, from its step_* events. */
export function StepIoPanel({ step, detail, onClose }: { step: WorkflowStep | null; detail: StepDetail; onClose: () => void }) {
  const type = step?.type ?? detail.stepType;
  return (
    <section data-testid="step-io-panel" data-step-id={detail.stepId} aria-label={`Step ${detail.stepId} input and output`} className="space-y-3 rounded-lg border border-border bg-card p-4">
      <div className="flex items-center justify-between gap-2">
        <h3 className="font-semibold text-card-foreground">
          <span className="font-mono">{detail.stepId}</span>
          {type ? <span className="ml-2 text-xs font-normal uppercase text-muted-foreground">{type}</span> : null}
        </h3>
        <button type="button" onClick={onClose} aria-label="Close step details" className="text-xs text-muted-foreground hover:text-foreground">Close</button>
      </div>
      <p>
        <span data-testid="step-io-state" className={`inline-block rounded border px-2 py-0.5 text-xs ${NODE_STATE_CLASSES[detail.state]}`}>{NODE_STATE_LABELS[detail.state]}</span>
        {detail.startedAt ? <span className="ml-2 text-xs text-muted-foreground">started {formatDateTime(detail.startedAt)}</span> : null}
      </p>
      {detail.childRunId ? (
        <p className="text-sm">
          <Link data-testid="step-child-run-link" href={`/runs/${detail.childRunId}`} className="text-primary underline-offset-4 hover:underline">
            Open agent run {detail.childRunId.slice(0, 8)}
          </Link>
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
          ) : detail.state === "waiting" ? <p className="text-xs text-muted-foreground">Decide above, in the approval panel.</p> : null}
        </div>
      ) : null}
      {detail.input !== undefined ? <Block label="Input" value={detail.input} testId="step-io-input" /> : null}
      {detail.hasOutput ? (
        type === "parallel" && detail.output === null ? (
          <div>
            <h4 className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">Output</h4>
            <p data-testid="step-io-output" className="mt-1 text-sm text-muted-foreground">No output (parallel step).</p>
          </div>
        ) : <Block label="Output" value={detail.output} testId="step-io-output" />
      ) : null}
      {detail.error || detail.errorCode ? (
        <div role="alert" data-testid="step-io-error" className="rounded-md border border-destructive/30 bg-destructive/10 p-2 text-sm text-destructive">
          {detail.error ? <p className="whitespace-pre-wrap">{detail.error}</p> : null}
          {detail.errorCode ? <p className="mt-1 text-xs">Error code: <code className="font-mono">{detail.errorCode}</code></p> : null}
        </div>
      ) : null}
      {!detail.started && detail.state === "pending" ? <p className="text-sm text-muted-foreground">This step has not started.</p> : null}
      {detail.state === "skipped" ? (
        <p data-testid="step-io-skip-reason" data-skip-reason={detail.skipReason ?? undefined} className="text-sm text-muted-foreground">
          {detail.skipReason === "run_ended" ? "Not reached: the run ended before this step." : "Skipped: this branch arm was not taken."}
        </p>
      ) : null}
    </section>
  );
}
