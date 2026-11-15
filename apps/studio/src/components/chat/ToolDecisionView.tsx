"use client";

import React from "react";
import { ShieldCheck, ShieldX } from "lucide-react";
import { ICON_STROKE_WIDTH } from "@/lib/icons";
import { DECISION_LABELS, type ToolDecision } from "./turnCapabilities";

export const LOST_RACE_MESSAGE = "Your decision was not recorded: this call had already been decided.";

/**
 * A gated call's recorded decision (AC.14 wording): "<tool>: Approved by <who>", the comment, and
 * the lost-race notice when this page's decision lost. Shared by the native ToolApprovalPart and the
 * AG-UI chat's tool cards.
 */
export function ToolDecisionView({ toolName, toolCallId, decision, lostRace = false }: { toolName: string; toolCallId: string; decision: ToolDecision; lostRace?: boolean }) {
  const approved = decision.kind === "approved";
  const Icon = approved ? ShieldCheck : ShieldX;
  return (
    <div
      data-testid="tool-approval-decision"
      data-decision={decision.kind}
      data-tool-call-id={toolCallId}
      className="my-2 flex min-w-0 items-start gap-2 rounded-md border border-border bg-surface px-3 py-2 text-caption"
    >
      <Icon strokeWidth={ICON_STROKE_WIDTH} aria-hidden="true" className={approved ? "mt-0.5 size-icon shrink-0 text-success-text" : "mt-0.5 size-icon shrink-0 text-warning-text"} />
      <div className="min-w-0 break-words">
        <p className="text-foreground">
          <span className="font-mono">{toolName}</span>: {DECISION_LABELS[decision.kind]}
          {decision.decidedBy && decision.kind !== "timed_out" ? (
            <>
              {" "}by <span className="font-mono">{decision.decidedBy}</span>
            </>
          ) : null}
        </p>
        {decision.comment ? <p className="mt-0.5 whitespace-pre-wrap text-muted-foreground">{decision.comment}</p> : null}
        {lostRace ? (
          <p role="status" data-testid="tool-approval-lost-race" className="mt-0.5 text-warning-text">
            {LOST_RACE_MESSAGE}
          </p>
        ) : null}
      </div>
    </div>
  );
}
