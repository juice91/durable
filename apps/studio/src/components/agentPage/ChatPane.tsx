"use client";

import React, { createContext, useContext } from "react";
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
          <MessagePrimitive.Parts components={{ Text: MarkdownText, tools: { Fallback: ToolCallCard } }} />
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
}

/**
 * The chat column: an empty conversation shows a landing (the agent's name) with the composer in the
 * middle; after the first send the composer docks at the bottom under the transcript. Turns are
 * grouped (space between turns, each turn's badge under its last message), and a "Scroll to bottom"
 * button appears once the transcript is scrolled up.
 */
export function ChatPane({ messages, disabled, running, onNew, turnViews, toolInfos, landingTitle, landingDescription, header, composer }: ChatPaneProps) {
  const runtime = useExternalStoreRuntime({
    messages,
    isDisabled: disabled,
    isRunning: running,
    onNew,
  });
  const empty = messages.length === 0;
  const turnStarts = React.useMemo(() => turnStartMessageIds(messages), [messages]);

  return (
    <AssistantRuntimeProvider runtime={runtime}>
      <TurnViewsContext.Provider value={turnViews}>
        <TurnStartsContext.Provider value={turnStarts}>
          <ToolCallInfoContext.Provider value={toolInfos}>
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
          </ToolCallInfoContext.Provider>
        </TurnStartsContext.Provider>
      </TurnViewsContext.Provider>
    </AssistantRuntimeProvider>
  );
}
