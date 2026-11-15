"use client";

import React from "react";
import { formatCost, formatDuration, formatTokens } from "@/lib/format";
import type { FailedAttempt, SpanKind, TraceSpan } from "@/lib/traces";
import { TraceLink } from "./TraceLink";

// AC.17: the detail sections of the agent capability spans (fallback attempts and the answering
// model, tool approval waits, output retries, eval cases and judge requests). They read the span's
// allowlisted attributes only: the API never puts provider error text, tool arguments or output
// values in them (a retry lists JSON paths, never values).

/** Attributes each section shows, left out of the generic attribute list. */
export const CAPABILITY_SHOWN: Partial<Record<SpanKind, readonly string[]>> = {
  model_request: ["answering_model", "requested_model_id", "fallback_index", "failed_attempts", "output_candidate"],
  fallback_attempt: ["request_key", "model_request", "fallback_position", "outcome", "error_type", "http_status", "timed"],
  tool_approval: ["tool_call_id", "tool_name", "deadline", "waiting_since", "waited", "decision"],
  output_retry: ["event_key", "model_request", "errors", "error_count", "cause"],
  eval_case: [
    "case_id", "case_status", "position", "assertions", "scores", "labels", "evaluator_failures", "duration_ms",
    "child_run_id",
  ],
  judge: ["request_key", "case_id", "evaluator_index"],
};

/** Capability kinds whose usage (model, tokens, cost) goes in the "Model and usage" section. */
export const CAPABILITY_USAGE_KINDS: ReadonlySet<SpanKind> = new Set<SpanKind>(["fallback_attempt", "judge"]);

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

function Section({
  title, testId, children, after,
}: { title: string; testId: string; children: React.ReactNode; after?: React.ReactNode }) {
  return (
    <section aria-label={title} data-testid={testId}>
      <h4 className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">{title}</h4>
      <dl className="mt-1 text-sm">{children}</dl>
      {after}
    </section>
  );
}

/** "ModelHTTPError, HTTP 503" (error type and status only). */
export function attemptFailure(attempt: FailedAttempt): string {
  const parts = [str(attempt.error_type) ?? (attempt.outcome === "rejected" ? "rejected" : "error")];
  const status = num(attempt.http_status);
  if (status !== null) parts.push(`HTTP ${status}`);
  return parts.join(", ");
}

function failedAttempts(value: unknown): FailedAttempt[] {
  return (Array.isArray(value) ? value : []).filter((a): a is FailedAttempt => !!a && typeof a === "object");
}

/** The answering model and fallback of a model request; null for a request without either. */
function FallbackSection({ span }: { span: TraceSpan }) {
  const a = span.attributes;
  const answering = str(a.answering_model);
  const index = num(a.fallback_index);
  const attempts = failedAttempts(a.failed_attempts);
  const candidate = a.output_candidate === true;
  if (!answering && index === null && !attempts.length && !candidate) return null;
  return (
    <Section title="Model chain" testId="span-fallback">
      {answering ? <Row label="Answering model" testId="span-answering-model">{answering}</Row> : null}
      {str(a.requested_model_id) ? <Row label="Requested model">{a.requested_model_id as string}</Row> : null}
      {index !== null ? (
        <Row label="Fallback" testId="span-fallback-index">
          {index === 0 ? "primary answered" : `fallback ${index} answered`}
        </Row>
      ) : null}
      {attempts.map((attempt, k) => (
        <Row key={k} label={`Failed attempt ${k + 1}`} testId="span-fallback-attempt">
          {`${str(attempt.model_id) ?? "unknown"}: ${attemptFailure(attempt)}`}
        </Row>
      ))}
      {candidate ? <Row label="Structured output" testId="span-output-candidate">candidate</Row> : null}
    </Section>
  );
}

function AttemptSection({ span }: { span: TraceSpan }) {
  const a = span.attributes;
  const k = num(a.fallback_position);
  return (
    <Section title="Failed attempt" testId="span-fallback-attempt-detail">
      {k !== null ? <Row label="Position in chain">{k + 1}</Row> : null}
      <Row label="Outcome">{str(a.outcome) ?? "-"}</Row>
      <Row label="Failure" testId="span-attempt-failure">{attemptFailure(a as FailedAttempt)}</Row>
      {num(a.model_request) !== null ? <Row label="Model request">{`#${a.model_request as number}`}</Row> : null}
      {a.timed === false ? (
        <p className="text-xs text-muted-foreground" data-testid="span-attempt-untimed">
          The response was not stored, so this attempt&apos;s timing is unknown.
        </p>
      ) : null}
    </Section>
  );
}

const DECISION_LABELS: Record<string, string> = {
  tool_approved: "approved",
  tool_denied: "denied",
  tool_approval_timed_out: "timed out",
};

function ApprovalSection({ span }: { span: TraceSpan }) {
  const a = span.attributes;
  const decision = (a.decision && typeof a.decision === "object" ? a.decision : null) as Record<string, unknown> | null;
  const decidedBy = decision ? str(decision.decided_by) : null;
  return (
    <Section title="Tool approval" testId="span-tool-approval">
      {str(a.tool_name) ? <Row label="Tool">{a.tool_name as string}</Row> : null}
      {str(a.deadline) ? <Row label="Deadline">{new Date(a.deadline as string).toLocaleString()}</Row> : null}
      {a.waited === false ? <Row label="Wait">none (decided at once)</Row> : null}
      <Row label="Decision" testId="span-approval-decision">
        {decision ? (DECISION_LABELS[String(decision.type)] ?? String(decision.type ?? "-")) : span.status === "waiting" ? "waiting" : "none"}
      </Row>
      {decidedBy ? <Row label="Decided by">{decidedBy === "eval" ? "eval (no approvals during evaluation)" : decidedBy}</Row> : null}
      {decision && str(decision.comment) ? <Row label="Comment">{decision.comment as string}</Row> : null}
    </Section>
  );
}

function RetrySection({ span }: { span: TraceSpan }) {
  const a = span.attributes;
  const errors = (Array.isArray(a.errors) ? a.errors : []).filter((e): e is string => typeof e === "string");
  return (
    <Section
      title="Output retry"
      testId="span-output-retry"
      after={
        errors.length ? (
          <ul className="mt-1 space-y-0.5 font-mono text-xs text-card-foreground" aria-label="Schema error paths" data-testid="span-output-retry-paths">
            {errors.map((path, i) => (
              <li key={`${path}-${i}`} className="break-all">{path}</li>
            ))}
          </ul>
        ) : null
      }
    >
      {num(a.model_request) !== null ? <Row label="Rejected candidate">{`model request #${a.model_request as number}`}</Row> : null}
      {str(a.cause) ? <Row label="Cause">{a.cause as string}</Row> : null}
      <Row label="Schema errors" testId="span-output-retry-count">{errors.length}</Row>
    </Section>
  );
}

function CaseSection({ span }: { span: TraceSpan }) {
  const a = span.attributes;
  const assertions = (a.assertions && typeof a.assertions === "object" ? a.assertions : null) as
    | { passed?: unknown; total?: unknown }
    | null;
  const passed = num(assertions?.passed);
  const total = num(assertions?.total);
  const duration = num(a.duration_ms);
  const childRun = str(a.child_run_id);
  return (
    <Section
      title="Test case"
      testId="span-eval-case"
      after={
        childRun ? (
          <TraceLink
            runId={childRun}
            tab={null}
            data-testid="span-link-case-run"
            className="mt-1 inline-block text-sm text-primary underline-offset-4 hover:underline"
          >
            Open case run trace
          </TraceLink>
        ) : null
      }
    >
      <Row label="Result" testId="span-eval-case-status">{str(a.case_status) ?? "-"}</Row>
      {num(a.position) !== null ? <Row label="Position">{(a.position as number) + 1}</Row> : null}
      {passed !== null && total !== null ? <Row label="Assertions" testId="span-eval-case-assertions">{`${passed} of ${total} passed`}</Row> : null}
      {num(a.scores) !== null ? <Row label="Scores">{formatTokens(a.scores as number)}</Row> : null}
      {num(a.evaluator_failures) ? <Row label="Evaluator failures">{formatTokens(a.evaluator_failures as number)}</Row> : null}
      {duration !== null ? <Row label="Duration">{formatDuration(duration)}</Row> : null}
      {"cost_usd" in a ? <Row label="Cost">{formatCost(str(a.cost_usd)) ?? "no price"}</Row> : null}
    </Section>
  );
}

function JudgeSection({ span }: { span: TraceSpan }) {
  const a = span.attributes;
  const index = num(a.evaluator_index);
  return (
    <Section title="Judge" testId="span-judge">
      {index !== null ? <Row label="Evaluator">{index + 1}</Row> : null}
      {str(a.request_key) ? <Row label="Request key">{a.request_key as string}</Row> : null}
    </Section>
  );
}

/** The capability section of a span, or null for kinds (and model requests) without one. */
export function CapabilitySpanSection({ span }: { span: TraceSpan }) {
  switch (span.kind) {
    case "model_request":
      return <FallbackSection span={span} />;
    case "fallback_attempt":
      return <AttemptSection span={span} />;
    case "tool_approval":
      return <ApprovalSection span={span} />;
    case "output_retry":
      return <RetrySection span={span} />;
    case "eval_case":
      return <CaseSection span={span} />;
    case "judge":
      return <JudgeSection span={span} />;
    default:
      return null;
  }
}
