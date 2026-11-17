"use client";

import React, { useEffect, useRef, useState } from "react";
import { ConfirmDialog } from "@/components/shared/ConfirmDialog";
import { ApiError } from "@/lib/api";
import { announceToolDecided, decideToolApproval, MAX_TOOL_APPROVAL_COMMENT, refreshPendingApprovals } from "@/lib/approvals";
import { decideApproval, MAX_APPROVAL_COMMENT, resumeStep } from "@/lib/workflows";
import { JsonSchemaForm } from "@/components/tools/JsonSchemaForm";
import { serverFieldErrors, validateAgainstSchema } from "@/lib/schemaValidation";
import type { JsonSchema } from "@/lib/tools";

// The one approval card (AC.14, moved from components/workflows/): a workflow approval step (the
// workflow run view's "Needs input" overlay), a gated tool call (the agent Chat tab's waiting turn)
// and the Approvals inbox (AC.15) all decide through it. Step decisions use the workflow routes
// (approve | reject); tool decisions use POST /runs/{id}/tool-approvals/{call}/approve | deny.

export const NOT_WAITING_MESSAGE = "This step is no longer waiting for approval (already decided, timed out, or the run ended). The view was refreshed.";
export const TOOL_NOT_WAITING_MESSAGE =
  "This tool call is no longer waiting for approval (already decided, timed out, or the run ended). The view was refreshed.";

export type ApprovalTarget =
  | { kind: "tool"; runId: string; toolCallId: string; toolName: string; args: unknown; deadline: string | null; /** What the call would do, in words (a tool with a registry summariser). */ summary?: string | null }
  | {
      kind: "step";
      runId: string;
      stepId: string;
      prompt: string | null;
      deadline: string | null;
      /** An input step: the JSON Schema of the data it waits for. The card then shows a form and
       * "Submit" (POST .../resume) instead of "Approve"; Reject stays. */
      resumeSchema?: JsonSchema | null;
      /** A step-by-step pause: titled "Paused after step", with Continue (approve) and Stop (reject). */
      pause?: boolean;
    };

export interface ApprovalCardProps {
  approval: ApprovalTarget;
  /** Reload the run's events (after a 409, or once a decision is recorded). The buttons stay
   * disabled until a returned promise settles, so a second click cannot race the refresh. */
  onRefresh: () => void | Promise<unknown>;
  /** A decision was recorded: "approved", or "denied" (a rejected step or a denied tool call). */
  onDecided?: (decision: "approved" | "denied") => void;
  /** Optional (AC.14 review): the decision lost a race (409). Called before the refresh, so a host
   * whose card unmounts once the refresh shows the recorded decision can still explain it. */
  onConflict?: (message: string) => void;
  /** Optional (AGUI.12): records the decision another way than the REST routes (the AG-UI chat
   * answers its interrupt with `resume`). Errors are shown like a REST failure. */
  submit?: (decision: "approve" | "deny", comment: string) => Promise<void>;
}

export function formatDateTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  const date = new Date(iso);
  return Number.isNaN(date.getTime()) ? iso : date.toLocaleString();
}

/** "in 59 min", "in 2 h 5 min", "in 30 s", or "passed" for a deadline; null when unknown. */
export function deadlineCountdown(iso: string | null | undefined, now: number): string | null {
  if (!iso) return null;
  const at = Date.parse(iso);
  if (Number.isNaN(at)) return null;
  const seconds = Math.round((at - now) / 1000);
  if (seconds <= 0) return "passed";
  if (seconds < 60) return `in ${seconds} s`;
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `in ${minutes} min`;
  const hours = Math.floor(minutes / 60);
  const rest = minutes % 60;
  return rest ? `in ${hours} h ${rest} min` : `in ${hours} h`;
}

/** The current time, re-read every `ms` while mounted (the countdown). */
function useNow(ms: number): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const timer = setInterval(() => setNow(Date.now()), ms);
    return () => clearInterval(timer);
  }, [ms]);
  return now;
}

function prettyJson(value: unknown): string {
  try {
    return JSON.stringify(value ?? {}, null, 2) ?? String(value);
  } catch {
    return String(value);
  }
}

/**
 * A waiting approval: the step's prompt, or the tool and its arguments; the deadline with a
 * countdown; an optional comment; Approve, and Reject (step) or Deny (tool) after a confirm.
 */
export function ApprovalCard({ approval, onRefresh, onDecided, onConflict, submit }: ApprovalCardProps) {
  const tool = approval.kind === "tool";
  const key = tool ? `tool-${approval.toolCallId}` : approval.stepId;
  const pause = approval.kind === "step" && approval.pause === true;
  const denyLabel = tool ? "Deny" : pause ? "Stop" : "Reject";
  const maxComment = tool ? MAX_TOOL_APPROVAL_COMMENT : MAX_APPROVAL_COMMENT;
  const resumeSchema = approval.kind === "step" ? approval.resumeSchema ?? null : null;
  const [formValue, setFormValue] = useState<Record<string, unknown>>({});
  const [fieldErrors, setFieldErrors] = useState<Record<string, string>>({});
  const [comment, setComment] = useState("");
  const [busy, setBusy] = useState<null | "approve" | "deny">(null);
  const [error, setError] = useState<string | null>(null);
  const [confirmDeny, setConfirmDeny] = useState(false);
  const [dialogError, setDialogError] = useState<string | null>(null);
  const tooLong = comment.length > maxComment;
  const commentId = `approval-comment-${key}`;
  const now = useNow(15_000);
  const countdown = deadlineCountdown(approval.deadline, now);
  const mounted = useRef(true);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  const decide = async (decision: "approve" | "deny") => {
    if (busy || tooLong) return;
    if (decision === "approve" && resumeSchema) {
      const problems = validateAgainstSchema(resumeSchema, formValue);
      setFieldErrors(problems);
      if (Object.keys(problems).length > 0) {
        setError("Fix the highlighted fields before submitting.");
        return;
      }
    }
    setBusy(decision);
    setError(null);
    setDialogError(null);
    try {
      if (submit) {
        await submit(decision, comment);
      } else if (approval.kind === "tool") {
        await decideToolApproval(approval.runId, approval.toolCallId, decision, comment);
      } else if (resumeSchema && decision === "approve" && approval.kind === "step") {
        await resumeStep(approval.runId, approval.stepId, formValue, comment);
      } else {
        await decideApproval(approval.runId, approval.stepId, decision === "approve" ? "approve" : "reject", comment);
      }
      setConfirmDeny(false);
      onDecided?.(decision === "approve" ? "approved" : "denied");
      if (tool) announceToolDecided(decision === "approve" ? "approved" : "denied");
      // The sidebar's pending count (AC.15) drops by one.
      void refreshPendingApprovals().catch(() => undefined);
      // Stay busy until the view has reloaded: the approval is no longer waiting, and a second
      // click before the card disappears would only earn a 409.
      await Promise.resolve(onRefresh()).catch(() => undefined);
    } catch (reason: unknown) {
      const status = reason instanceof ApiError ? reason.status : null;
      let message: string;
      if (status === 409) {
        message = tool ? TOOL_NOT_WAITING_MESSAGE : NOT_WAITING_MESSAGE;
        setConfirmDeny(false);
        onConflict?.(message);
        void refreshPendingApprovals().catch(() => undefined);
        onRefresh();
      } else if (status === 404) {
        message = tool
          ? "Tool call not found: the run may have been removed, or it belongs to another tenant."
          : "Run not found: it may have been removed, or it belongs to another tenant.";
        setConfirmDeny(false);
      } else if (status === 422 && resumeSchema && reason instanceof ApiError) {
        setFieldErrors(serverFieldErrors(reason.details));
        message = "The server rejected some fields; fix them and submit again.";
      } else {
        message = reason instanceof Error && reason.message ? reason.message : "The decision could not be recorded; try again.";
      }
      if (decision === "deny" && status !== 409 && status !== 404) setDialogError(message);
      else setError(message);
    } finally {
      if (mounted.current) setBusy(null);
    }
  };

  const subject = tool ? approval.toolName : approval.stepId;
  return (
    <div
      data-testid={`approval-${key}`}
      data-kind={approval.kind}
      role="region"
      aria-label={tool ? `Approval for tool ${approval.toolName}` : `Approval for step ${approval.stepId}`}
      className="min-w-0 space-y-3 rounded-lg border-2 border-warning bg-card p-4"
    >
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <h3 className="min-w-0 break-words font-semibold text-foreground">
          {tool ? "Approve tool call: " : pause ? "Paused after step: " : resumeSchema ? "Waiting for input: " : "Waiting for approval: "}
          <span className="font-mono">{pause ? subject.replace(/_pause$/, "") : subject}</span>
        </h3>
        <p className="text-xs text-muted-foreground">
          Deadline:{" "}
          <time data-testid={`approval-deadline-${key}`} dateTime={approval.deadline ?? undefined}>
            {formatDateTime(approval.deadline)}
          </time>
          {countdown ? (
            <span data-testid={`approval-countdown-${key}`} className="ml-1 tabular-nums">
              ({countdown})
            </span>
          ) : null}
        </p>
      </div>
      {tool ? (
        <div>
          {approval.summary ? (
            <p data-testid={`approval-summary-${key}`} className="mb-2 whitespace-pre-wrap text-sm font-medium text-foreground">
              {approval.summary}
            </p>
          ) : null}
          <p className="mb-1 text-caption font-medium text-muted-foreground">Arguments</p>
          <pre
            data-testid={`approval-args-${key}`}
            className="max-h-64 overflow-auto whitespace-pre-wrap break-words rounded-sm bg-fill-hover p-2 font-mono text-caption text-foreground"
          >
            {prettyJson(approval.args)}
          </pre>
        </div>
      ) : (
        <p data-testid={`approval-prompt-${key}`} className="whitespace-pre-wrap text-sm text-foreground">
          {approval.prompt ?? "(no prompt)"}
        </p>
      )}
      {resumeSchema ? (
        <JsonSchemaForm schema={resumeSchema} value={formValue} onChange={setFormValue} errors={fieldErrors} idPrefix={`resume-${key}`} />
      ) : null}
      <div className="space-y-1">
        <label htmlFor={commentId} className="block text-sm font-medium text-foreground">
          Comment (optional{tool ? "; a denial's comment is shown to the model" : ""})
        </label>
        <textarea
          id={commentId}
          rows={2}
          value={comment}
          aria-invalid={tooLong}
          aria-describedby={`${commentId}-count`}
          onChange={(event) => setComment(event.target.value)}
          className="w-full rounded-md border border-input bg-background p-2 text-sm text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        />
        <p id={`${commentId}-count`} className={`text-xs ${tooLong ? "text-destructive-text" : "text-muted-foreground"}`}>
          {comment.length}/{maxComment}
          {tooLong ? " — too long" : ""}
        </p>
      </div>
      {error ? (
        <p role="alert" data-testid={`approval-error-${key}`} className="rounded-md border border-destructive/30 bg-destructive/10 p-2 text-sm text-destructive-text">
          {error}
        </p>
      ) : null}
      <div className="flex flex-wrap gap-2">
        <button
          type="button"
          onClick={() => decide("approve")}
          disabled={Boolean(busy) || tooLong}
          aria-busy={busy === "approve"}
          className="rounded-md bg-primary px-4 py-1.5 text-sm font-medium text-primary-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:cursor-not-allowed disabled:opacity-50"
        >
          {resumeSchema ? "Submit" : pause ? "Continue" : "Approve"}
        </button>
        <button
          type="button"
          onClick={() => {
            setDialogError(null);
            setConfirmDeny(true);
          }}
          disabled={Boolean(busy) || tooLong}
          className="rounded-md border border-destructive px-4 py-1.5 text-sm font-medium text-destructive-text focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:cursor-not-allowed disabled:opacity-50"
        >
          {denyLabel}
        </button>
      </div>
      {confirmDeny ? (
        <ConfirmDialog
          title={tool ? `Deny ${approval.toolName}?` : pause ? "Stop this run?" : `Reject step ${approval.stepId}?`}
          description={
            tool
              ? "The tool does not run. The model is told the call was denied (with your comment) and carries on."
              : pause
                ? "Stopping ends the run as cancelled. This cannot be undone."
                : "Rejecting fails the workflow run with approval_rejected. This cannot be undone."
          }
          confirmLabel={pause ? "Stop run" : denyLabel}
          destructive
          busy={busy === "deny"}
          error={dialogError}
          onConfirm={() => decide("deny")}
          onCancel={() => setConfirmDeny(false)}
        />
      ) : null}
    </div>
  );
}
