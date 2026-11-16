"use client";

import React, { useEffect, useState } from "react";
import Link from "next/link";
import { MessageSquare, ScrollText, Workflow } from "lucide-react";
import { ICON_STROKE_WIDTH } from "@/lib/icons";
import { getRun } from "@/lib/runs";
import {
  APPROVAL_KIND_LABELS,
  approvalLinks,
  approvalSubject,
  isDecidable,
  type Approval,
} from "@/lib/approvals";
import { Button } from "@/components/ui/Button";
import { Sheet, SheetBody, SheetContent, SheetHeader } from "@/components/ui/Sheet";
import { useFrame } from "@/components/frame/FrameContext";
import { useViewportWidth } from "@/components/frame/useMediaQuery";
import { TraceLink } from "@/components/traces/TraceLink";
import { logSheetWidth } from "@/components/traces/traceSheetModel";
import { ApprovalStatusBadge } from "./ApprovalStatusBadge";
import { InboxApprovalCard } from "./InboxApprovalCard";

const linkClass =
  "inline-flex h-7 items-center gap-1.5 rounded-md border border-border px-2 text-label font-medium text-foreground hover:bg-fill-hover focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring";

function Field({ label, children, testId }: { label: string; children: React.ReactNode; testId?: string }) {
  return (
    <div className="grid min-w-0 grid-cols-[7rem_minmax(0,1fr)] gap-2 py-1">
      <dt className="text-caption text-muted-foreground">{label}</dt>
      <dd className="min-w-0 break-all text-caption text-foreground" data-testid={testId}>{children}</dd>
    </div>
  );
}

function when(iso: string | null): string {
  if (!iso) return "-";
  const date = new Date(iso);
  return Number.isNaN(date.getTime()) ? iso : date.toLocaleString();
}

/** The run's conversation id (tool calls outside a workflow link to their agent conversation). */
function useConversationId(approval: Approval | null): string | null {
  const runId = approval && approval.kind === "tool" && !approval.workflow_id ? approval.run_id : null;
  const [state, setState] = useState<{ runId: string; conversationId: string | null } | null>(null);
  useEffect(() => {
    if (!runId) return;
    let live = true;
    getRun(runId)
      .then((run) => {
        if (live) setState({ runId, conversationId: run.conversation_id ?? null });
      })
      .catch(() => {
        if (live) setState({ runId, conversationId: null });
      });
    return () => {
      live = false;
    };
  }, [runId]);
  return state && state.runId === runId ? state.conversationId : null;
}

export interface ApprovalSheetProps {
  /** The open approval, or null when closed. */
  approval: Approval | null;
  /** The URL names an approval the inbox could not find (decided long ago, another tenant, a typo). */
  missing?: boolean;
  /** Why the lookup of a missing approval failed (network, 5xx); null for a plain 404. */
  missingError?: string | null;
  agentName?: string | null;
  onOpenChange: (open: boolean) => void;
  onDecided: (approval: Approval, decision: "approved" | "denied") => void;
  /** The card lost a race (409): re-fetch the item and explain. The Sheet stays open. */
  onConflict: (approval: Approval) => void | Promise<unknown>;
  /** Shown above Details after a lost race: who decided first, and that this decision did not count. */
  notice?: string | null;
  /** Try a failed lookup again. */
  onRetryLookup?: () => void;
}

/**
 * One approval (AC.15), bound to `?approval=<run_id>:<key>`: what is asked, the decision controls
 * (the shared ApprovalCard) while it is pending, the recorded decision once decided, and links to
 * the agent conversation, the workflow run view, and the trace Sheet ("Open trace" stacks it over
 * this Sheet).
 */
export function ApprovalSheet({ approval, missing = false, missingError = null, agentName = null, onOpenChange, onDecided, onConflict, notice = null, onRetryLookup }: ApprovalSheetProps) {
  const open = approval !== null || missing;
  const frameState = useFrame();
  const viewport = useViewportWidth(open);
  const width = logSheetWidth({ viewport, sidebarCollapsed: frameState?.sidebarCollapsed ?? false });
  const conversationId = useConversationId(approval);
  const links = approval ? approvalLinks(approval, { conversationId }) : null;
  return (
    <Sheet open={open} onOpenChange={onOpenChange}>
      {open ? (
        <SheetContent size="md" style={width === null ? undefined : { width }} data-testid="approval-sheet">
          {approval && links ? (
            <>
              <SheetHeader
                title={`${APPROVAL_KIND_LABELS[approval.kind]}: ${approvalSubject(approval)}`}
                description={approval.kind === "tool" ? (agentName ? `Agent ${agentName}` : "Agent tool call") : "Workflow approval step"}
              />
              <SheetBody className="flex flex-col gap-4">
                <div className="flex flex-wrap items-center gap-2">
                  <ApprovalStatusBadge approval={approval} testId="approval-sheet-status" />
                  <span className="ml-auto flex flex-wrap gap-2">
                    {links.conversation ? (
                      <Link href={links.conversation} className={linkClass} data-testid="approval-sheet-open-conversation">
                        <MessageSquare className="size-icon-dense" strokeWidth={ICON_STROKE_WIDTH} aria-hidden="true" />
                        Open conversation
                      </Link>
                    ) : null}
                    {links.workflowRun ? (
                      <Link href={links.workflowRun} className={linkClass} data-testid="approval-sheet-open-workflow-run">
                        <Workflow className="size-icon-dense" strokeWidth={ICON_STROKE_WIDTH} aria-hidden="true" />
                        Open workflow run
                      </Link>
                    ) : null}
                    <TraceLink runId={approval.run_id} className={linkClass} data-testid="approval-sheet-open-trace">
                      <ScrollText className="size-icon-dense" strokeWidth={ICON_STROKE_WIDTH} aria-hidden="true" />
                      Open trace
                    </TraceLink>
                  </span>
                </div>

                {isDecidable(approval) ? (
                  <InboxApprovalCard
                    // A new card per approval: its comment and busy state belong to one decision.
                    key={`${approval.run_id}:${approval.approval_key}`}
                    approval={approval}
                    onConflict={() => onConflict(approval)}
                    onDecided={(decision) => onDecided(approval, decision)}
                  />
                ) : (
                  <>
                    {approval.prompt ? (
                      <section aria-label="Prompt">
                        <h3 className="mb-1 text-caption font-medium text-muted-foreground">Prompt</h3>
                        <p className="whitespace-pre-wrap text-label text-foreground [overflow-wrap:anywhere]">{approval.prompt}</p>
                      </section>
                    ) : null}
                    {approval.kind === "tool" ? (
                      <section aria-label="Arguments">
                        <h3 className="mb-1 text-caption font-medium text-muted-foreground">Arguments</h3>
                        <pre data-testid="approval-sheet-args" className="max-h-[40vh] overflow-auto whitespace-pre-wrap rounded-md bg-muted p-3 font-mono text-caption text-foreground [overflow-wrap:anywhere]">
                          {JSON.stringify(approval.args ?? {}, null, 2)}
                        </pre>
                      </section>
                    ) : null}
                  </>
                )}

                {notice ? (
                  <p role="status" data-testid="approval-sheet-notice" className="rounded-md border border-warning/40 bg-warning/10 p-2 text-label text-warning-text">
                    {notice}
                  </p>
                ) : null}

                <section aria-label="Details">
                  <h3 className="mb-1 text-caption font-medium text-muted-foreground">Details</h3>
                  <dl className="divide-y divide-border">
                    <Field label="Requested"><time dateTime={approval.requested_at ?? undefined}>{when(approval.requested_at)}</time></Field>
                    <Field label="Deadline"><time dateTime={approval.deadline ?? undefined}>{when(approval.deadline)}</time></Field>
                    {approval.decided_at ? (
                      <Field label="Decided" testId="approval-sheet-decided">
                        <time dateTime={approval.decided_at}>{when(approval.decided_at)}</time>
                        {approval.decision === "timed_out" ? " (timeout)" : approval.decided_by ? ` by ${approval.decided_by}` : ""}
                      </Field>
                    ) : null}
                    {approval.decided_at && approval.comment ? <Field label="Comment" testId="approval-sheet-comment">{approval.comment}</Field> : null}
                    {approval.decided_at && approval.resume_data != null ? (
                      <Field label="Submitted data" testId="approval-sheet-resume-data">
                        <pre className="max-h-48 overflow-auto whitespace-pre-wrap break-words font-mono text-caption">{JSON.stringify(approval.resume_data, null, 2)}</pre>
                      </Field>
                    ) : null}
                    <Field label="Run"><span className="font-mono">{approval.run_id}</span></Field>
                    {approval.root_run_id !== approval.run_id ? <Field label="Workflow run"><span className="font-mono">{approval.root_run_id}</span></Field> : null}
                    {approval.kind === "tool" ? <Field label="Tool call"><span className="font-mono">{approval.approval_key}</span></Field> : null}
                    {approval.run_status ? <Field label="Run status">{approval.run_status.replace(/_/g, " ")}</Field> : null}
                  </dl>
                </section>
              </SheetBody>
            </>
          ) : (
            <>
              <SheetHeader title={missingError ? "Approval could not be loaded" : "Approval not found"} />
              <SheetBody>
                {missingError ? (
                  <div className="flex flex-col items-start gap-2">
                    <p role="alert" data-testid="approval-sheet-error" className="rounded-md border border-destructive/30 bg-destructive/10 p-2 text-label text-destructive-text">
                      {missingError}
                    </p>
                    {onRetryLookup ? (
                      <Button variant="secondary" size="sm" onClick={onRetryLookup} data-testid="approval-sheet-retry">
                        Retry
                      </Button>
                    ) : null}
                  </div>
                ) : (
                  <p data-testid="approval-sheet-missing" className="text-label text-muted-foreground">
                    No approval has this link. It may belong to another tenant, or its run may have been deleted.
                  </p>
                )}
              </SheetBody>
            </>
          )}
        </SheetContent>
      ) : null}
    </Sheet>
  );
}
