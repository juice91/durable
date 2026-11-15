import React from "react";
import { Badge } from "@/components/ui/Badge";
import { APPROVAL_DECISION_LABELS, TERMINAL_RUN_STATUSES, type Approval } from "@/lib/approvals";

/** Pending (warning), Run ended (neutral: never decided, its run is over), or the recorded decision: approved (success), denied (destructive), timed out (neutral). */
export function ApprovalStatusBadge({ approval, testId }: { approval: Pick<Approval, "status" | "decision" | "run_status">; testId?: string }) {
  if ((approval.decision === null || approval.status === "pending") && TERMINAL_RUN_STATUSES.has(approval.run_status ?? "")) {
    // Never decided, and its run ended: nothing waits any more.
    return <Badge variant="neutral" data-testid={testId}>Run ended</Badge>;
  }
  if (approval.decision === null && approval.status === "decided") {
    // Known only to be no longer waiting (a lost race whose item could not be re-read).
    return <Badge variant="neutral" data-testid={testId}>No longer waiting</Badge>;
  }
  if (approval.decision === null || approval.status === "pending") {
    return <Badge variant="warning" data-testid={testId}>Pending</Badge>;
  }
  const variant = approval.decision === "approved" ? "success" : approval.decision === "denied" ? "destructive" : "neutral";
  return <Badge variant={variant} data-testid={testId}>{APPROVAL_DECISION_LABELS[approval.decision]}</Badge>;
}
