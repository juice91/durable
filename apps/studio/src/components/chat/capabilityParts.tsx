"use client";

import React, { createContext, useContext } from "react";
import { useAuiState, type DataMessagePartProps } from "@assistant-ui/react";
import { RotateCcw, ShieldCheck, ShieldX } from "lucide-react";
import { ICON_STROKE_WIDTH } from "@/lib/icons";
import { ApprovalCard } from "@/components/approvals/ApprovalCard";
import { StructuredOutputView } from "./StructuredOutput";
import { useTurnSources } from "./messageParts";
import { messageRunId, toolCallInfoKey } from "./toolCalls";
import { DECISION_LABELS, outputRetryOf, outputRetryText, type ToolApprovalState } from "./turnCapabilities";

// The chat's data parts for the agent capabilities milestone (thread.ts emits them): the structured
// answer, a rejected-candidate marker, and the inline tool approval card of a waiting turn.

export { OUTPUT_RETRY_PART, STRUCTURED_OUTPUT_PART, TOOL_APPROVAL_PART } from "./turnCapabilities";

/** What a chat data part may ask of the chat: re-read one turn's events (after a decision or a 409). */
export interface ChatRunActions {
  refreshRun: (runId: string) => Promise<void>;
  /** Gated calls (`toolCallInfoKey(runId, callId)`) whose decision from this page lost a race (409):
   * the decision view says so after the card is gone. */
  lostRaces?: ReadonlySet<string>;
  noteLostRace?: (key: string) => void;
}

export const ChatRunActionsContext = createContext<ChatRunActions>({ refreshRun: async () => undefined });

export const LOST_RACE_MESSAGE = "Your decision was not recorded: this call had already been decided.";

/** The validated object of a structured turn, with `[n]` linked to the turn's retrieve sources. */
export function StructuredOutputPart({ data }: DataMessagePartProps<{ value: unknown }>) {
  const sources = useTurnSources();
  return <StructuredOutputView value={data?.value} sources={sources} />;
}

/** A rejected structured-output candidate: the model was asked again (`output_retry`). */
export function OutputRetryPart({ data }: DataMessagePartProps<Record<string, unknown>>) {
  const retry = outputRetryOf(data ?? {});
  return (
    <p
      data-testid="output-retry"
      data-model-request={retry.modelRequest ?? undefined}
      className="my-2 flex min-w-0 items-start gap-2 rounded-md border border-border bg-muted px-3 py-2 text-caption text-muted-foreground"
    >
      <RotateCcw strokeWidth={ICON_STROKE_WIDTH} aria-hidden="true" className="mt-0.5 size-icon shrink-0" />
      <span className="min-w-0 break-words">
        <span className="font-medium text-foreground">Output retry. </span>
        {outputRetryText(retry)}
      </span>
    </p>
  );
}

/** A gated tool call of the turn: the shared ApprovalCard while it waits, its decision after. */
export function ToolApprovalPart({ data: raw }: DataMessagePartProps<ToolApprovalState>) {
  const data = raw as ToolApprovalState | undefined;
  const messageId = useAuiState((s) => s.message.id);
  const runId = messageRunId(messageId);
  const { refreshRun, lostRaces, noteLostRace } = useContext(ChatRunActionsContext);
  if (!data || runId === null) return null;
  if (data.decision) {
    const approved = data.decision.kind === "approved";
    const Icon = approved ? ShieldCheck : ShieldX;
    return (
      <div
        data-testid="tool-approval-decision"
        data-decision={data.decision.kind}
        data-tool-call-id={data.toolCallId}
        className="my-2 flex min-w-0 items-start gap-2 rounded-md border border-border bg-surface px-3 py-2 text-caption"
      >
        <Icon strokeWidth={ICON_STROKE_WIDTH} aria-hidden="true" className={approved ? "mt-0.5 size-icon shrink-0 text-success-text" : "mt-0.5 size-icon shrink-0 text-warning-text"} />
        <div className="min-w-0 break-words">
          <p className="text-foreground">
            <span className="font-mono">{data.toolName}</span>: {DECISION_LABELS[data.decision.kind]}
            {data.decision.decidedBy && data.decision.kind !== "timed_out" ? (
              <>
                {" "}by <span className="font-mono">{data.decision.decidedBy}</span>
              </>
            ) : null}
          </p>
          {data.decision.comment ? <p className="mt-0.5 whitespace-pre-wrap text-muted-foreground">{data.decision.comment}</p> : null}
          {lostRaces?.has(toolCallInfoKey(runId, data.toolCallId)) ? (
            <p role="status" data-testid="tool-approval-lost-race" className="mt-0.5 text-warning-text">
              {LOST_RACE_MESSAGE}
            </p>
          ) : null}
        </div>
      </div>
    );
  }
  return (
    <div className="my-2" data-testid="tool-approval-waiting" data-tool-call-id={data.toolCallId}>
      <ApprovalCard
        approval={{ kind: "tool", runId, toolCallId: data.toolCallId, toolName: data.toolName, args: data.args, deadline: data.deadline }}
        onRefresh={() => refreshRun(runId)}
        onConflict={() => noteLostRace?.(toolCallInfoKey(runId, data.toolCallId))}
      />
    </div>
  );
}
