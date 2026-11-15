"use client";

import React, { useCallback, useRef } from "react";
import type { Approval } from "@/lib/approvals";
import { ApprovalCard } from "./ApprovalCard";

export interface InboxApprovalCardProps {
  approval: Approval;
  /**
   * The card refreshed without recording a decision: a 409 (decided elsewhere, timed out, or the
   * run ended). The inbox re-fetches the item and says who got there first.
   */
  onConflict: () => void | Promise<unknown>;
  onDecided: (decision: "approved" | "denied") => void;
}

/**
 * The inbox's only dependency on the shared `ApprovalCard` (AC.14): maps an inbox item to the
 * card's `approval` prop, a tool call or a workflow step.
 */
export function InboxApprovalCard({ approval, onConflict, onDecided }: InboxApprovalCardProps) {
  // The card refreshes after a recorded decision too. The inbox has already updated the row in
  // place (onDecided), and a reload would drop it from the Pending list, so only a refresh without
  // a decision (a 409: decided elsewhere, timed out, the run ended) goes to the inbox.
  const decided = useRef(false);
  const handleDecided = useCallback(
    (decision: "approved" | "denied") => {
      decided.current = true;
      onDecided(decision);
    },
    [onDecided],
  );
  const handleRefresh = useCallback(() => (decided.current ? undefined : onConflict()), [onConflict]);
  const card =
    approval.kind === "tool"
      ? {
          kind: "tool" as const,
          runId: approval.run_id,
          toolCallId: approval.tool_call_id ?? approval.approval_key,
          toolName: approval.tool_name ?? approval.approval_key,
          args: approval.args,
          deadline: approval.deadline,
        }
      : {
          kind: "step" as const,
          runId: approval.run_id,
          stepId: approval.step_id ?? approval.approval_key,
          prompt: approval.prompt,
          deadline: approval.deadline,
        };
  return <ApprovalCard approval={card} onRefresh={handleRefresh} onDecided={handleDecided} />;
}
