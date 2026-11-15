"use client";

import React, { createContext, useContext, useEffect } from "react";
import {
  AssistantRuntimeProvider,
  MessagePrimitive,
  ThreadPrimitive,
  useAuiState,
  useExternalStoreRuntime,
  type AppendMessage,
  type ThreadMessage as ThreadMessageLike,
} from "@assistant-ui/react";
import { ArrowDown } from "lucide-react";
import { cn } from "@/lib/cn";
import { ICON_STROKE_WIDTH } from "@/lib/icons";
import { focusRing } from "@/components/ui/Button";
import { TurnFooter, type TurnView } from "@/components/chat/TurnFooter";
import { MarkdownText, ToolCallCard, ToolCallInfoContext } from "@/components/chat/messageParts";
import { messageRunId, type ToolCallInfo } from "@/components/chat/toolCalls";
import {
  ChatRunActionsContext,
  OUTPUT_RETRY_PART,
  OutputRetryPart,
  STRUCTURED_OUTPUT_PART,
  StructuredOutputPart,
  TOOL_APPROVAL_PART,
  ToolApprovalPart,
  type ChatRunActions,
} from "@/components/chat/capabilityParts";

export { messageRunId };

/** Thread message id → the turn whose badge and notices render under it (its last message). */
const TurnViewsContext = createContext<ReadonlyMap<string, TurnView>>(new Map());
/** Thread message ids that open a turn after the first: they get the space between turns. */
const TurnStartsContext = createContext<ReadonlySet<string>>(new Set());


/** Thread message id of each turn's last message → its run id. */
export function footerMessageIds(messages: readonly { id: string }[]): Map<string, string> {
  const lastByRun = new Map<string, string>();
  for (const message of messages) {
    const runId = messageRunId(message.id);
    if (runId) lastByRun.set(runId, message.id);
  }
  return new Map(Array.from(lastByRun, ([runId, messageId]) => [messageId, runId]));
}

/** The first message of every turn but the first (turn grouping: space goes between turns). */
export function turnStartMessageIds(messages: readonly { id: string }[]): Set<string> {
  const starts = new Set<string>();
  let previous: string | null = null;
  for (const message of messages) {
    const runId = messageRunId(message.id);
    if (runId !== previous && previous !== null) starts.add(message.id);
    previous = runId;
  }
  return starts;
}

/** AC.14: the structured answer, the output retry marker and the inline tool approval card. */
const DATA_PARTS = {
  by_name: {
    [STRUCTURED_OUTPUT_PART]: StructuredOutputPart,
    [OUTPUT_RETRY_PART]: OutputRetryPart,
    [TOOL_APPROVAL_PART]: ToolApprovalPart,
  },
};

const NO_ACTIONS: ChatRunActions = { refreshRun: async () => undefined };

function ChatMessage() {
  const messageId = useAuiState((s) => s.message.id);
  const turn = useContext(TurnViewsContext).get(messageId);
  const turnStart = useContext(TurnStartsContext).has(messageId);
  return (
    <MessagePrimitive.Root className={cn("flex w-full min-w-0 flex-col", turnStart ? "mt-8" : "mt-3 first:mt-0")} data-turn-start={turnStart || undefined}>
      <MessagePrimitive.If user>
        <div data-testid="chat-message-user" className="ml-auto max-w-[85%] min-w-0 break-words rounded-lg bg-fill-selected px-3 py-2 text-body text-foreground">
          <MessagePrimitive.Parts components={{ tools: { Fallback: ToolCallCard } }} />
        </div>
      </MessagePrimitive.If>
      <MessagePrimitive.If assistant>
        <div data-testid="chat-message-assistant" className="mr-auto w-full min-w-0 text-foreground">
          <MessagePrimitive.Parts components={{ Text: MarkdownText, tools: { Fallback: ToolCallCard }, data: DATA_PARTS }} />
        </div>
      </MessagePrimitive.If>
      {turn ? <TurnFooter turn={turn} /> : null}
    </MessagePrimitive.Root>
  );
}

export interface ChatPaneProps {
  messages: readonly ThreadMessageLike[];
  disabled: boolean;
  running: boolean;
  onNew: (message: AppendMessage) => Promise<void>;
  turnViews: ReadonlyMap<string, TurnView>;
  toolInfos: ReadonlyMap<string, ToolCallInfo>;
  /** The empty conversation's heading (the agent's name). */
  landingTitle: string;
  landingDescription?: string | null;
  /** Run status, alerts and notices, above the conversation. */
  header?: React.ReactNode;
  /** The composer (rendered inside the thread runtime). */
  composer: React.ReactNode;
  /** Text to put back in the composer (a refused send); a new `key` puts it back again. */
  restoreDraft?: { text: string; key: number } | null;
  /** Called once `restoreDraft` is in the composer, so the caller drops it (a remount of the pane,
   * when the window crosses 1024 px, must not put old text back). */
  onDraftRestored?: () => void;
  /** Re-read one turn's events (the approval card, after a decision or a 409; AC.14). */
  onRefreshRun?: (runId: string) => Promise<void>;
}

/**
 * The chat column: an empty conversation shows a landing (the agent's name) with the composer in the
 * middle; after the first send the composer docks at the bottom under the transcript. Turns are
 * grouped (space between turns, each turn's badge under its last message), and a "Scroll to bottom"
 * button appears once the transcript is scrolled up.
 */
export function ChatPane({ messages, disabled, running, onNew, turnViews, toolInfos, landingTitle, landingDescription, header, composer, restoreDraft = null, onDraftRestored, onRefreshRun }: ChatPaneProps) {
  // Lost approval races (409) of this page, kept while the pane lives: the inline card is gone by
  // the time the refresh shows the recorded decision.
  const [lostRaces, setLostRaces] = React.useState<ReadonlySet<string>>(() => new Set());
  const noteLostRace = React.useCallback((key: string) => setLostRaces((current) => (current.has(key) ? current : new Set(current).add(key))), []);
  const actions = React.useMemo<ChatRunActions>(
    () => ({ refreshRun: onRefreshRun ?? NO_ACTIONS.refreshRun, lostRaces, noteLostRace }),
    [onRefreshRun, lostRaces, noteLostRace],
  );
  const runtime = useExternalStoreRuntime({
    messages,
    isDisabled: disabled,
    isRunning: running,
    onNew,
  });
  // assistant-ui empties the composer when it sends; a refused send puts the text back.
  const restoredRef = React.useRef(onDraftRestored);
  restoredRef.current = onDraftRestored;
  useEffect(() => {
    if (!restoreDraft) return;
    runtime.thread.composer.setText(restoreDraft.text);
    restoredRef.current?.();
  }, [restoreDraft, runtime]);
  const empty = messages.length === 0;
  const turnStarts = React.useMemo(() => turnStartMessageIds(messages), [messages]);

  return (
    <AssistantRuntimeProvider runtime={runtime}>
      <TurnViewsContext.Provider value={turnViews}>
        <TurnStartsContext.Provider value={turnStarts}>
          <ToolCallInfoContext.Provider value={toolInfos}>
          <ChatRunActionsContext.Provider value={actions}>
            <ThreadPrimitive.Root data-testid="chat-pane" data-empty={empty || undefined} className="flex min-h-0 min-w-0 flex-1 flex-col">
              {header ? <div className="mx-auto w-full min-w-0 max-w-3xl shrink-0 px-4 pt-3">{header}</div> : null}
              <ThreadPrimitive.Viewport className="flex min-h-0 min-w-0 flex-1 flex-col overflow-y-auto px-4">
                {empty ? (
                  <div data-testid="chat-landing" className="mx-auto mt-auto w-full min-w-0 max-w-3xl pb-4 text-center">
                    <h2 className="text-display font-semibold text-foreground">{landingTitle}</h2>
                    <p className="mt-1 text-body text-muted-foreground">{landingDescription || "Start a conversation by sending a message."}</p>
                  </div>
                ) : (
                  <div data-testid="chat-transcript" className="mx-auto w-full min-w-0 max-w-3xl py-4">
                    <ThreadPrimitive.Messages components={{ Message: ChatMessage }} />
                  </div>
                )}
                <ThreadPrimitive.ViewportFooter
                  data-testid="chat-composer-dock"
                  data-docked={empty ? "false" : "true"}
                  className={cn("mx-auto w-full min-w-0 max-w-3xl", empty ? "mb-auto pb-4" : "sticky bottom-0 mt-auto bg-surface pb-4 pt-1")}
                >
                  {empty ? null : (
                    <div className="pointer-events-none flex justify-center pb-2">
                      <ThreadPrimitive.ScrollToBottom
                        aria-label="Scroll to bottom"
                        data-testid="chat-scroll-to-bottom"
                        className={cn(
                          "pointer-events-auto inline-flex size-8 items-center justify-center rounded-full border border-border bg-surface-raised text-muted-foreground shadow-raised hover:text-foreground disabled:invisible",
                          focusRing,
                        )}
                      >
                        <ArrowDown aria-hidden="true" strokeWidth={ICON_STROKE_WIDTH} className="size-icon" />
                      </ThreadPrimitive.ScrollToBottom>
                    </div>
                  )}
                  {composer}
                </ThreadPrimitive.ViewportFooter>
              </ThreadPrimitive.Viewport>
            </ThreadPrimitive.Root>
          </ChatRunActionsContext.Provider>
          </ToolCallInfoContext.Provider>
        </TurnStartsContext.Provider>
      </TurnViewsContext.Provider>
    </AssistantRuntimeProvider>
  );
}
