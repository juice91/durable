"use client";

import React, { createContext, useContext, useEffect } from "react";
import {
  AssistantRuntimeProvider,
  ExportedMessageRepository,
  MessagePrimitive,
  ThreadPrimitive,
  useAuiState,
  useExternalStoreRuntime,
  type AppendMessage,
  type AssistantRuntime,
  type TextMessagePartProps,
  type ThreadHistoryAdapter,
  type ThreadMessage as ThreadMessageLike,
} from "@assistant-ui/react";
import { useAgUiRuntime } from "@assistant-ui/react-ag-ui";
import { ArrowDown } from "lucide-react";
import { cn } from "@/lib/cn";
import { ICON_STROKE_WIDTH } from "@/lib/icons";
import { focusRing } from "@/components/ui/Button";
import { TurnFooter, type TurnView } from "@/components/chat/TurnFooter";
import { MarkdownText, MessageRunsContext, ToolCallCard, ToolCallInfoContext, ToolDecisionsContext, useMessageRunId, useTurnSources, type ToolDecisionEntry } from "@/components/chat/messageParts";
import { StructuredOutputView } from "@/components/chat/StructuredOutput";
import { MessageAttachmentList } from "@/components/chat/Attachments";
import { createAttachmentAdapter } from "@/components/chat/attachmentAdapter";
import type { ModelCapabilities } from "@/lib/attachments";
import { FeedbackScopeContext, MessageFeedback, isRatableMessage, type FeedbackScope } from "@/components/chat/MessageFeedback";
import { AgUiApprovals, type AgUiApprovalLookups } from "@/components/chat/AgUiApprovals";
import { messageRunMap, type RunMaps, type StudioChatAgent } from "@/components/chat/agUiChat";
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


type RunOf = (message: { id: string }) => string | null | undefined;
const parseRun: RunOf = (message) => messageRunId(message.id);

/** Thread message id of each turn's last message → its run id (`runOf`: default, the id's prefix). */
export function footerMessageIds(messages: readonly { id: string }[], runOf: RunOf = parseRun): Map<string, string> {
  const lastByRun = new Map<string, string>();
  for (const message of messages) {
    const runId = runOf(message);
    if (runId) lastByRun.set(runId, message.id);
  }
  return new Map(Array.from(lastByRun, ([runId, messageId]) => [messageId, runId]));
}

/** The first message of every turn but the first (turn grouping: space goes between turns). */
export function turnStartMessageIds(messages: readonly { id: string }[], runOf: RunOf = parseRun): Set<string> {
  const starts = new Set<string>();
  let previous: string | null = null;
  for (const message of messages) {
    const runId = runOf(message) ?? null;
    if (runId !== previous && previous !== null) starts.add(message.id);
    previous = runId;
  }
  return starts;
}

/**
 * Structured output of the AG-UI chat (AGUI.12): native run id → the validated object (from
 * `RUN_FINISHED.result`, else the run's `output_json`). The turn's assistant text is that object's
 * canonical JSON (D13); it renders with the AC.14 view instead of as text.
 */
export const StructuredResultsContext = createContext<ReadonlyMap<string, unknown>>(new Map());

/** JSON with object keys sorted, so two encodings of one value compare equal. */
function stableJson(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(stableJson).join(",")}]`;
  if (value && typeof value === "object") {
    const entries = Object.entries(value as Record<string, unknown>).sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0));
    return `{${entries.map(([key, item]) => `${JSON.stringify(key)}:${stableJson(item)}`).join(",")}}`;
  }
  return JSON.stringify(value) ?? "null";
}

/** Whether `text` is the canonical JSON of `value` (an object, array or scalar: `3`, `"ok"`, `true`). */
export function isJsonOf(text: string, value: unknown): boolean {
  try {
    return stableJson(JSON.parse(text.trim())) === stableJson(value);
  } catch {
    return false;
  }
}

/** Output retries per native run id (AG-UI: from the run's native `output_retry` events). */
export const OutputRetriesContext = createContext<ReadonlyMap<string, readonly Record<string, unknown>[]>>(new Map());

/** Assistant text: Markdown, or the AC.14 structured view for a structured turn's JSON answer. */
function ChatText(props: TextMessagePartProps) {
  const runId = useMessageRunId();
  const results = useContext(StructuredResultsContext);
  const sources = useTurnSources();
  if (runId !== null && results.has(runId) && isJsonOf(props.text, results.get(runId))) {
    return <StructuredOutputView value={results.get(runId)} sources={sources} />;
  }
  return <MarkdownText {...props} />;
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
  const settled = useAuiState((s) => s.message.status?.type !== "running");
  const turn = useContext(TurnViewsContext).get(messageId);
  const turnStart = useContext(TurnStartsContext).has(messageId);
  const allRetries = useContext(OutputRetriesContext);
  // AG-UI: the turn's output retries lead its last message (before the structured answer).
  const retries = turn ? allRetries.get(turn.runId) : undefined;
  return (
    <MessagePrimitive.Root className={cn("flex w-full min-w-0 flex-col", turnStart ? "mt-8" : "mt-3 first:mt-0")} data-turn-start={turnStart || undefined}>
      <MessagePrimitive.If user>
        <div data-testid="chat-message-user" className="ml-auto max-w-[85%] min-w-0 break-words rounded-lg bg-fill-selected px-3 py-2 text-body text-foreground">
          <MessageAttachmentList />
          <MessagePrimitive.Parts components={{ tools: { Fallback: ToolCallCard } }} />
        </div>
      </MessagePrimitive.If>
      <MessagePrimitive.If assistant>
        <div data-testid="chat-message-assistant" className="mr-auto w-full min-w-0 text-foreground">
          {retries?.map((retry, index) => <OutputRetryPart key={index} {...({ data: retry } as React.ComponentProps<typeof OutputRetryPart>)} />)}
          <MessagePrimitive.Parts components={{ Text: ChatText, tools: { Fallback: ToolCallCard }, data: DATA_PARTS }} />
          {settled && isRatableMessage(messageId) ? <MessageFeedback messageId={messageId} /> : null}
        </div>
      </MessagePrimitive.If>
      {turn ? <TurnFooter turn={turn} /> : null}
    </MessagePrimitive.Root>
  );
}

/** What both chat runtimes share. */
export interface ChatPaneCommonProps {
  disabled: boolean;
  /** Native run id → the turn's badge and notices (under its last message). */
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

export interface ChatPaneProps extends ChatPaneCommonProps {
  messages: readonly ThreadMessageLike[];
  running: boolean;
  onNew: (message: AppendMessage) => Promise<void>;
}

function useRunActions(onRefreshRun: ((runId: string) => Promise<void>) | undefined): ChatRunActions {
  // Lost approval races (409) of this page, kept while the pane lives: the inline card is gone by
  // the time the refresh shows the recorded decision.
  const [lostRaces, setLostRaces] = React.useState<ReadonlySet<string>>(() => new Set());
  const noteLostRace = React.useCallback((key: string) => setLostRaces((current) => (current.has(key) ? current : new Set(current).add(key))), []);
  return React.useMemo<ChatRunActions>(
    () => ({ refreshRun: onRefreshRun ?? NO_ACTIONS.refreshRun, lostRaces, noteLostRace }),
    [onRefreshRun, lostRaces, noteLostRace],
  );
}

/** assistant-ui empties the composer when it sends; a refused send puts the text back. */
function useRestoreDraft(runtime: AssistantRuntime, restoreDraft: { text: string; key: number } | null, onDraftRestored: (() => void) | undefined) {
  const restoredRef = React.useRef(onDraftRestored);
  restoredRef.current = onDraftRestored;
  useEffect(() => {
    if (!restoreDraft) return;
    runtime.thread.composer.setText(restoreDraft.text);
    restoredRef.current?.();
  }, [restoreDraft, runtime]);
}

interface ChatPaneBodyProps extends Pick<ChatPaneCommonProps, "turnViews" | "toolInfos" | "landingTitle" | "landingDescription" | "header" | "composer"> {
  actions: ChatRunActions;
  /** Message id → native run id; null: parse the ids (`<runId>:...`, the native runtime). */
  messageRuns: ReadonlyMap<string, string> | null;
  /** Shown after the transcript, above the composer (the AG-UI approval cards). */
  afterMessages?: React.ReactNode;
}

/**
 * The chat column: an empty conversation shows a landing (the agent's name) with the composer in the
 * middle; after the first send the composer docks at the bottom under the transcript. Turns are
 * grouped (space between turns, each turn's badge under its last message), and a "Scroll to bottom"
 * button appears once the transcript is scrolled up. Rendered inside either runtime's provider.
 */
function ChatPaneBody({ turnViews, toolInfos, landingTitle, landingDescription, header, composer, actions, messageRuns, afterMessages }: ChatPaneBodyProps) {
  const messages = useAuiState((s) => s.thread.messages);
  const empty = messages.length === 0;
  const runOf = React.useCallback<RunOf>((message) => (messageRuns ? messageRuns.get(message.id) : messageRunId(message.id)), [messageRuns]);
  const turnStarts = React.useMemo(() => turnStartMessageIds(messages, runOf), [messages, runOf]);
  const viewsByMessage = React.useMemo(() => {
    const views = new Map<string, TurnView>();
    for (const [messageId, runId] of footerMessageIds(messages, runOf)) {
      const view = turnViews.get(runId);
      if (view) views.set(messageId, view);
    }
    return views;
  }, [messages, runOf, turnViews]);

  return (
    <MessageRunsContext.Provider value={messageRuns}>
      <TurnViewsContext.Provider value={viewsByMessage}>
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
                    {afterMessages}
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
    </MessageRunsContext.Provider>
  );
}

/** The chat column on the native runtime (`thread.ts` messages over `/runs/{id}/stream`). */
export function ChatPane({ messages, disabled, running, onNew, turnViews, toolInfos, landingTitle, landingDescription, header, composer, restoreDraft = null, onDraftRestored, onRefreshRun }: ChatPaneProps) {
  const actions = useRunActions(onRefreshRun);
  const runtime = useExternalStoreRuntime({
    messages,
    isDisabled: disabled,
    isRunning: running,
    onNew,
  });
  useRestoreDraft(runtime, restoreDraft, onDraftRestored);
  return (
    <AssistantRuntimeProvider runtime={runtime}>
      <ChatPaneBody
        turnViews={turnViews}
        toolInfos={toolInfos}
        landingTitle={landingTitle}
        landingDescription={landingDescription}
        header={header}
        composer={composer}
        actions={actions}
        messageRuns={null}
      />
    </AssistantRuntimeProvider>
  );
}

export interface AgUiChatPaneProps extends ChatPaneCommonProps {
  /** The thread's agent (one per thread: mount a new pane, by `key`, for another thread). */
  agent: StudioChatAgent;
  /** The history adapter (`agUiThreadHistory`), made once with the agent. */
  history: ThreadHistoryAdapter;
  /** What ties the runtime's messages to native runs (D13 id prefixes, the user messages sent). */
  runMaps: RunMaps;
  /** Native run id → its structured output. */
  structured: ReadonlyMap<string, unknown>;
  /** The runtime started or stopped a run (or an attach). */
  onRunningChange?: (running: boolean) => void;
  /** Open interrupts, for the composer (a new message is refused while they are open). */
  onInterruptsChange?: (count: number) => void;
  /** A runtime failure (the chat shows the refusal from the agent's listener; this is the rest). */
  onError?: (error: Error) => void;
  /** A new `key` drops the transcript's last user message and what follows it (a refused send). */
  dropLastTurn?: { key: number } | null;
  approvals: AgUiApprovalLookups;
  /** `toolCallInfoKey(runId, toolCallId)` → a gated call's decision line (under its tool card). */
  decisions?: ReadonlyMap<string, ToolDecisionEntry>;
  /** Native run id → its output retries (the AC.14 marker). */
  retries?: ReadonlyMap<string, readonly Record<string, unknown>[]>;
  /** The conversation and the caller's thumbs on it (W2.4); without a conversation no thumbs show. */
  feedback?: FeedbackScope;
  /** What the selected model takes (GET /models), for the attach picker and its refusals. */
  attachmentCapabilities?: ModelCapabilities | null;
}

const NO_DECISIONS: ReadonlyMap<string, ToolDecisionEntry> = new Map();
const NO_FEEDBACK: FeedbackScope = { conversationId: null, initial: {} };
const NO_RETRIES: ReadonlyMap<string, readonly Record<string, unknown>[]> = new Map();

/** Status and interrupt counts reported from inside the runtime provider. */
function RuntimeReporter({ onRunningChange, onInterruptsChange, interrupts }: { onRunningChange?: (running: boolean) => void; onInterruptsChange?: (count: number) => void; interrupts: number }) {
  const running = useAuiState((s) => s.thread.isRunning);
  useEffect(() => {
    onRunningChange?.(running);
  }, [onRunningChange, running]);
  useEffect(() => {
    onInterruptsChange?.(interrupts);
  }, [interrupts, onInterruptsChange]);
  return null;
}

/**
 * The chat column on AG-UI (AGUI.12, D21): `useAgUiRuntime` with the thread's `StudioChatAgent`
 * (`ResumableHttpAgent`), the thread GET as history (`unstable_resume` while a turn runs), and
 * `resumeTranscript: "appended"`. Tool approvals come from interrupts (`AgUiApprovals`, the shared
 * ApprovalCard answered by `resume`); structured output renders from `RUN_FINISHED.result`.
 */
export function AgUiChatPane({
  agent,
  history,
  runMaps,
  structured,
  disabled,
  turnViews,
  toolInfos,
  landingTitle,
  landingDescription,
  header,
  composer,
  restoreDraft = null,
  onDraftRestored,
  onRefreshRun,
  onRunningChange,
  onInterruptsChange,
  onError,
  dropLastTurn = null,
  approvals,
  decisions = NO_DECISIONS,
  retries = NO_RETRIES,
  feedback = NO_FEEDBACK,
  attachmentCapabilities = null,
}: AgUiChatPaneProps) {
  const actions = useRunActions(onRefreshRun);
  // W2.4: files attached in the composer are uploaded first and sent as AG-UI media parts.
  // The adapter is made once; it reads the selected model's capabilities as they are at each attach.
  const capabilitiesRef = React.useRef(attachmentCapabilities);
  capabilitiesRef.current = attachmentCapabilities;
  const attachments = React.useMemo(() => createAttachmentAdapter(undefined, () => capabilitiesRef.current), []);
  const adapters = React.useMemo(() => ({ history, attachments }), [history, attachments]);
  const errorRef = React.useRef(onError);
  errorRef.current = onError;
  const reportError = React.useCallback((error: Error) => errorRef.current?.(error), []);
  const runtime = useAgUiRuntime({
    agent,
    adapters,
    resumeTranscript: "appended",
    showThinking: false,
    isDisabled: disabled,
    onError: reportError,
  });
  useRestoreDraft(runtime, restoreDraft, onDraftRestored);
  const [interrupts, setInterrupts] = React.useState(0);
  const [running, setRunning] = React.useState(false);
  const handleRunning = React.useCallback(
    (next: boolean) => {
      setRunning(next);
      onRunningChange?.(next);
    },
    [onRunningChange],
  );

  // A refused send: its user message (and the failed answer) leave the transcript once the run has
  // settled, as the native chat drops a refused turn.
  const dropRef = React.useRef<number | null>(null);
  useEffect(() => {
    if (dropLastTurn) dropRef.current = dropLastTurn.key;
  }, [dropLastTurn]);
  useEffect(() => {
    if (running || dropRef.current === null || !dropLastTurn || dropRef.current !== dropLastTurn.key) return;
    dropRef.current = null;
    const messages = runtime.thread.getState().messages;
    let last = -1;
    for (let index = messages.length - 1; index >= 0; index -= 1) {
      if (messages[index].role === "user") {
        last = index;
        break;
      }
    }
    if (last < 0) return;
    runtime.thread.import(ExportedMessageRepository.fromArray(messages.slice(0, last)));
  }, [dropLastTurn, running, runtime]);

  return (
    <AssistantRuntimeProvider runtime={runtime}>
      <RuntimeReporter onRunningChange={handleRunning} onInterruptsChange={onInterruptsChange} interrupts={interrupts} />
      <StructuredResultsContext.Provider value={structured}>
      <ToolDecisionsContext.Provider value={decisions}>
      <OutputRetriesContext.Provider value={retries}>
      <FeedbackScopeContext.Provider value={feedback}>
        <AgUiBody
          runMaps={runMaps}
          turnViews={turnViews}
          toolInfos={toolInfos}
          landingTitle={landingTitle}
          landingDescription={landingDescription}
          header={header}
          composer={composer}
          actions={actions}
          approvals={approvals}
          onInterrupts={setInterrupts}
          onRefreshRun={onRefreshRun}
        />
      </FeedbackScopeContext.Provider>
      </OutputRetriesContext.Provider>
      </ToolDecisionsContext.Provider>
      </StructuredResultsContext.Provider>
    </AssistantRuntimeProvider>
  );
}

function AgUiBody({
  runMaps,
  approvals,
  onInterrupts,
  onRefreshRun,
  ...body
}: Omit<ChatPaneBodyProps, "messageRuns" | "afterMessages"> & {
  runMaps: RunMaps;
  approvals: AgUiApprovalLookups;
  onInterrupts: (count: number) => void;
  onRefreshRun?: (runId: string) => Promise<void>;
}) {
  const messages = useAuiState((s) => s.thread.messages);
  const messageRuns = React.useMemo(() => messageRunMap(messages, runMaps), [messages, runMaps]);
  return (
    <ChatPaneBody
      {...body}
      messageRuns={messageRuns}
      afterMessages={
        <AgUiApprovals lookups={approvals} onCount={onInterrupts} onRefreshRun={onRefreshRun} />
      }
    />
  );
}
