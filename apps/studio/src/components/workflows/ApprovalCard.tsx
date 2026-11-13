"use client";

import React, { useState } from "react";
import { ConfirmDialog } from "@/components/shared/ConfirmDialog";
import { ApiError } from "@/lib/api";
import { decideApproval, MAX_APPROVAL_COMMENT, type ApprovalDecisionResult } from "@/lib/workflows";

export const NOT_WAITING_MESSAGE = "This step is no longer waiting for approval (already decided, timed out, or the run ended). The view was refreshed.";

interface ApprovalCardProps {
  runId: string;
  stepId: string;
  prompt: string | null;
  deadline: string | null;
  /** Reload the run's events (after a 409, or once a decision is recorded). */
  onRefresh: () => void;
  onDecided?: (result: ApprovalDecisionResult) => void;
}

export function formatDateTime(iso: string | null | undefined): string {
  if (!iso) return "—";
  const date = new Date(iso);
  return Number.isNaN(date.getTime()) ? iso : date.toLocaleString();
}

/** A waiting approval step: its prompt and deadline, an optional comment, Approve and Reject. */
export function ApprovalCard({ runId, stepId, prompt, deadline, onRefresh, onDecided }: ApprovalCardProps) {
  const [comment, setComment] = useState("");
  const [busy, setBusy] = useState<null | "approve" | "reject">(null);
  const [error, setError] = useState<string | null>(null);
  const [confirmReject, setConfirmReject] = useState(false);
  const [dialogError, setDialogError] = useState<string | null>(null);
  const tooLong = comment.length > MAX_APPROVAL_COMMENT;
  const commentId = `approval-comment-${stepId}`;

  const decide = async (decision: "approve" | "reject") => {
    if (busy || tooLong) return;
    setBusy(decision);
    setError(null);
    setDialogError(null);
    try {
      const result = await decideApproval(runId, stepId, decision, comment);
      setConfirmReject(false);
      onDecided?.(result);
      onRefresh();
    } catch (reason: unknown) {
      const status = reason instanceof ApiError ? reason.status : null;
      let message: string;
      if (status === 409) {
        message = NOT_WAITING_MESSAGE;
        setConfirmReject(false);
        onRefresh();
      } else if (status === 404) {
        message = "Run not found: it may have been removed, or it belongs to another tenant.";
        setConfirmReject(false);
      } else {
        message = reason instanceof Error && reason.message ? reason.message : "The decision could not be recorded; try again.";
      }
      if (decision === "reject" && status !== 409 && status !== 404) setDialogError(message);
      else setError(message);
    } finally {
      setBusy(null);
    }
  };

  return (
    <div data-testid={`approval-${stepId}`} role="region" aria-label={`Approval for step ${stepId}`} className="space-y-3 rounded-lg border-2 border-yellow-500 bg-card p-4">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <h3 className="font-semibold text-foreground">Waiting for approval: <span className="font-mono">{stepId}</span></h3>
        <p className="text-xs text-muted-foreground">Deadline: <time data-testid={`approval-deadline-${stepId}`} dateTime={deadline ?? undefined}>{formatDateTime(deadline)}</time></p>
      </div>
      <p data-testid={`approval-prompt-${stepId}`} className="whitespace-pre-wrap text-sm text-foreground">{prompt ?? "(no prompt)"}</p>
      <div className="space-y-1">
        <label htmlFor={commentId} className="block text-sm font-medium text-foreground">Comment (optional)</label>
        <textarea
          id={commentId}
          rows={2}
          value={comment}
          aria-invalid={tooLong}
          aria-describedby={`${commentId}-count`}
          onChange={(event) => setComment(event.target.value)}
          className="w-full rounded-md border border-input bg-background p-2 text-sm text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        />
        <p id={`${commentId}-count`} className={`text-xs ${tooLong ? "text-destructive" : "text-muted-foreground"}`}>
          {comment.length}/{MAX_APPROVAL_COMMENT}{tooLong ? " — too long" : ""}
        </p>
      </div>
      {error ? <p role="alert" data-testid={`approval-error-${stepId}`} className="rounded-md border border-destructive/30 bg-destructive/10 p-2 text-sm text-destructive">{error}</p> : null}
      <div className="flex gap-2">
        <button type="button" onClick={() => decide("approve")} disabled={Boolean(busy) || tooLong} aria-busy={busy === "approve"} className="rounded-md bg-primary px-4 py-1.5 text-sm font-medium text-primary-foreground disabled:cursor-not-allowed disabled:opacity-50">
          Approve
        </button>
        <button type="button" onClick={() => { setDialogError(null); setConfirmReject(true); }} disabled={Boolean(busy) || tooLong} className="rounded-md border border-destructive px-4 py-1.5 text-sm font-medium text-destructive disabled:cursor-not-allowed disabled:opacity-50">
          Reject
        </button>
      </div>
      {confirmReject ? (
        <ConfirmDialog
          title={`Reject step ${stepId}?`}
          description="Rejecting fails the workflow run with approval_rejected. This cannot be undone."
          confirmLabel="Reject"
          destructive
          busy={busy === "reject"}
          error={dialogError}
          onConfirm={() => decide("reject")}
          onCancel={() => setConfirmReject(false)}
        />
      ) : null}
    </div>
  );
}
