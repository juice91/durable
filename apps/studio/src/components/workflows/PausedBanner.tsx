"use client";

import { usePermissions } from "@/lib/permissions";
import { PermissionNotice } from "@/components/access/PermissionNotice";
import React, { useEffect, useRef, useState } from "react";
import { ConfirmDialog } from "@/components/shared/ConfirmDialog";
import { Badge } from "@/components/ui/Badge";
import { ApiError } from "@/lib/api";
import { noteApprovalDecided, refreshPendingApprovals } from "@/lib/approvals";
import { decideApproval } from "@/lib/workflows";
import { formatDateTime } from "@/components/approvals/ApprovalCard";

/** The step a pause id (`<step>_pause`) follows. */
export function pausedAfter(pauseStepId: string): string {
  return pauseStepId.replace(/_pause$/, "");
}

interface PausedBannerProps {
  /** The run's workflow: `approve` on it decides who may continue or stop. */
  workflowId?: string | null;
  runId: string;
  /** The synthetic waiting step id, `<step>_pause`. */
  stepId: string;
  deadline: string | null;
  onRefresh: () => void | Promise<unknown>;
  onDecided?: () => void;
}

const APPROVE_ACTION = ["approve"] as const;

/**
 * A step-by-step run waiting after a step: Continue approves the pause step, Stop rejects it (the
 * run fails with cancelled_by_user) after a confirm. The step's output is open beside it.
 */
export function PausedBanner({ runId, stepId, deadline, onRefresh, onDecided, workflowId = null }: PausedBannerProps) {
  const permissions = usePermissions("workflow", workflowId ?? undefined, APPROVE_ACTION);
  const mayDecide = !workflowId || permissions.can("approve");
  const step = pausedAfter(stepId);
  const [busy, setBusy] = useState<null | "approve" | "reject">(null);
  const [error, setError] = useState<string | null>(null);
  const [confirmStop, setConfirmStop] = useState(false);
  const mounted = useRef(true);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  const decide = async (decision: "approve" | "reject") => {
    if (busy) return;
    setBusy(decision);
    setError(null);
    try {
      await decideApproval(runId, stepId, decision);
      setConfirmStop(false);
      onDecided?.();
      noteApprovalDecided();
      void refreshPendingApprovals().catch(() => undefined);
      await Promise.resolve(onRefresh()).catch(() => undefined);
    } catch (reason: unknown) {
      const status = reason instanceof ApiError ? reason.status : null;
      if (status === 409) {
        setConfirmStop(false);
        setError("This run is no longer paused (already continued, timed out, or ended). The view was refreshed.");
        void Promise.resolve(onRefresh()).catch(() => undefined);
      } else {
        setError(reason instanceof Error && reason.message ? reason.message : "The decision could not be recorded; try again.");
      }
    } finally {
      if (mounted.current) setBusy(null);
    }
  };

  return (
    <div data-testid="workflow-paused-banner" role="region" aria-label={`Paused after ${step}`} className="min-w-0 space-y-2 rounded-md bg-card p-3">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <p className="min-w-0 break-words font-semibold text-foreground">
          Paused after <span className="font-mono">{step}</span>
        </p>
        {deadline ? <p className="text-caption text-muted-foreground">Auto-stops: <time dateTime={deadline}>{formatDateTime(deadline)}</time></p> : null}
      </div>
      <p className="text-caption text-muted-foreground">The output of {step} is open in the step panel. Continue runs the next step.</p>
      {error ? <p role="alert" data-testid="workflow-paused-error" className="rounded-md border border-destructive/30 bg-destructive/10 p-2 text-sm text-destructive-text">{error}</p> : null}
      {mayDecide ? (
        <div className="flex flex-wrap gap-2">
          <button type="button" onClick={() => void decide("approve")} disabled={busy !== null} aria-busy={busy === "approve"} className="rounded-md bg-primary px-4 py-1.5 text-sm font-medium text-primary-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:cursor-not-allowed disabled:opacity-50">
            Continue
          </button>
          <button type="button" onClick={() => setConfirmStop(true)} disabled={busy !== null} className="rounded-md border border-destructive px-4 py-1.5 text-sm font-medium text-destructive-text focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:cursor-not-allowed disabled:opacity-50">
            Stop
          </button>
        </div>
      ) : workflowId ? (
        <PermissionNotice kind="workflow" id={workflowId} actions={APPROVE_ACTION} />
      ) : null}
      {confirmStop ? (
        <ConfirmDialog
          title="Stop this run?"
          description="Stopping ends the run as cancelled. This cannot be undone."
          confirmLabel="Stop run"
          destructive
          busy={busy === "reject"}
          onConfirm={() => void decide("reject")}
          onCancel={() => setConfirmStop(false)}
        />
      ) : null}
    </div>
  );
}
