"use client";

import React, { useRef, useState } from "react";
import { ApiError } from "@/lib/api";
import { cancelRun, type RunCancelResult } from "@/lib/workflows";
import { Button } from "@/components/ui/Button";
import { toast } from "@/components/ui/Toaster";
import { ConfirmDialog } from "@/components/shared/ConfirmDialog";

/** The dialog's text when a cancel is already recorded but not finished (a 503, or still running). */
export const FINISH_CANCEL_DESCRIPTION =
  "A cancel was already requested for this run but has not finished. It completes automatically; finishing it now stops whatever is still running at once.";

export const CANCEL_DESCRIPTION =
  "Every step still running stops, and the run ends as cancelled. Steps that already finished keep their results; side effects they made are not undone.";

/** What a failed cancel tells the person. */
export function cancelErrorMessage(reason: unknown): string {
  if (reason instanceof ApiError) {
    if (reason.status === 503) return "The cancel was recorded but has not finished yet. It completes automatically; confirming again finishes it now.";
    if (reason.status === 404) return "Run not found.";
    if (reason.message && !/^HTTP \d+$/.test(reason.message)) return reason.message;
    return `The run could not be cancelled (HTTP ${reason.status}).`;
  }
  return reason instanceof Error ? reason.message : "The run could not be cancelled.";
}

export interface CancelRunButtonProps {
  runId: string;
  /** A cancel is already recorded (`cancel_requested`) but the run has not ended yet. */
  cancelRequested?: boolean;
  /** Re-read the run after the cancel (or a 409: it ended meanwhile). */
  onSettled?: (result: RunCancelResult | null) => void;
}

/**
 * Cancel a running workflow run (loops D12), behind a confirmation. The caller renders it only while
 * the run is not terminal. While a recorded cancel is still finishing it reads "Cancelling…".
 */
export function CancelRunButton({ runId, cancelRequested = false, onSettled }: CancelRunButtonProps) {
  const [confirming, setConfirming] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);

  const close = () => {
    setConfirming(false);
    setError(null);
  };
  const confirm = async () => {
    setBusy(true);
    setError(null);
    try {
      const result = await cancelRun(runId);
      setConfirming(false);
      toast(result.cancelled ? "Run cancelled" : "The run had already ended");
      onSettled?.(result);
    } catch (reason) {
      if (reason instanceof ApiError && reason.status === 409) {
        // Already ended (or not a workflow run): nothing to cancel; show what it is now.
        setConfirming(false);
        toast(cancelErrorMessage(reason));
        onSettled?.(null);
      } else {
        setError(cancelErrorMessage(reason));
      }
    } finally {
      setBusy(false);
    }
  };

  return (
    <>
      <Button
        ref={triggerRef}
        size="sm"
        variant="secondary"
        data-testid="workflow-run-cancel"
        disabled={busy}
        title={cancelRequested ? "A cancel is recorded but not finished: select to finish it now" : undefined}
        onClick={() => setConfirming(true)}
      >
        {cancelRequested ? "Cancelling…" : "Cancel run"}
      </Button>
      {confirming ? (
        // A recorded cancel stays clickable: retrying finishes it at once (the API is idempotent).
        <ConfirmDialog
          title={cancelRequested ? "Finish cancelling this run?" : "Cancel this run?"}
          description={<p>{cancelRequested ? FINISH_CANCEL_DESCRIPTION : CANCEL_DESCRIPTION}</p>}
          confirmLabel={cancelRequested ? "Finish cancelling" : "Cancel run"}
          cancelLabel="Keep running"
          destructive
          busy={busy}
          error={error}
          onConfirm={() => void confirm()}
          onCancel={close}
          fallbackFocus={() => triggerRef.current}
        />
      ) : null}
    </>
  );
}
