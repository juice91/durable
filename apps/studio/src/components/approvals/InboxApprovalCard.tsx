"use client";

import React, { useCallback, useRef } from "react";
import type { Approval } from "@/lib/approvals";
import Link from "next/link";
import { routes } from "@/lib/routes";
import { ApprovalCard } from "./ApprovalCard";

const SLUG = /^[a-z][a-z0-9-]{1,47}$/;

/** True for an architect.* call whose arguments name an agent or workflow by slug id. */
function namesArchitectDefinition(toolName: string | null, args: unknown): boolean {
  if (!toolName?.startsWith("architect.") || typeof args !== "object" || args === null) return false;
  const record = args as Record<string, unknown>;
  return [record.agent_id, record.workflow_id, record.id].some((value) => typeof value === "string" && SLUG.test(value));
}

/** The workflow the approval runs under, else its agent (the order the API checks). */
function subjectOf(approval: Approval) {
  if (approval.workflow_id) return { kind: "workflow" as const, id: approval.workflow_id };
  return approval.agent_id ? { kind: "agent" as const, id: approval.agent_id } : null;
}

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
          summary: approval.summary ?? null,
        }
      : {
          kind: "step" as const,
          runId: approval.run_id,
          stepId: approval.step_id ?? approval.approval_key,
          prompt: approval.prompt,
          deadline: approval.deadline,
          resumeSchema: approval.resume_schema ?? null,
          pause: approval.pause === true,
        };
  const showLink = approval.kind === "tool" && namesArchitectDefinition(approval.tool_name, approval.args);
  return (
    <>
      <ApprovalCard approval={card} subject={subjectOf(approval)} onRefresh={handleRefresh} onDecided={handleDecided} />
      {showLink ? (
        <Link data-testid="approval-architect-link" href={routes.architect()} className="mt-2 inline-block text-label underline underline-offset-4">
          Open in Architect
        </Link>
      ) : null}
    </>
  );
}
