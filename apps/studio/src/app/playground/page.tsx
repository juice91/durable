"use client";

import React, {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import {
  AssistantRuntimeProvider,
  ComposerPrimitive,
  MessagePrimitive,
  ThreadPrimitive,
  useExternalStoreRuntime,
  type AppendMessage,
  type TextMessagePartProps,
  type ToolCallMessagePartProps,
} from "@assistant-ui/react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import {
  conversationToThread,
  type ThreadTurn,
} from "@/components/playground/thread";
import { StatusPill } from "@/components/runs/StatusPill";
import { api, type Agent } from "@/lib/api";
import {
  createConversation,
  getConversation,
  startTurn,
} from "@/lib/conversations";
import {
  deltaReducer,
  listEvents,
  subscribeRun,
  type RunStatus,
  type SubscribeHandlers,
} from "@/lib/runs";

type PartialTextMap = Record<string, { text: string; attempt: string }>;

/** One turn of the conversation: a run plus what the thread needs to draw it. */
type Turn = ThreadTurn & {
  displayStatus: RunStatus;
  error: string | null;
};

const TERMINAL_STATUSES = new Set<RunStatus>([
  "completed",
  "failed",
  "needs_attention",
]);

function failedEventStatus(status: unknown): "failed" | "needs_attention" {
  return status === "needs_attention" ? "needs_attention" : "failed";
}

function messageText(message: AppendMessage): string {
  return message.content
    .filter((part): part is Extract<typeof part, { type: "text" }> =>
      part.type === "text",
    )
    .map((part) => part.text)
    .join("\n")
    .trim();
}

function ToolCall({ toolName, args, result }: ToolCallMessagePartProps) {
  return (
    <div className="my-2 rounded-md border border-border bg-card p-3 text-card-foreground">
      <p className="text-sm font-semibold">{toolName}</p>
      <pre className="mt-2 overflow-x-auto text-xs text-muted-foreground">
        {JSON.stringify(args, null, 2)}
      </pre>
      {result !== undefined && (
        <pre className="mt-2 overflow-x-auto border-t border-border pt-2 text-xs">
          {JSON.stringify(result, null, 2)}
        </pre>
      )}
    </div>
  );
}

function MarkdownText({ text }: TextMessagePartProps) {
  return (
    <div className="space-y-2 break-words [&_a]:text-blue-600 [&_a]:underline [&_code]:rounded [&_code]:bg-background/60 [&_code]:px-1 [&_pre]:overflow-x-auto [&_ul]:list-disc [&_ul]:pl-5">
      <ReactMarkdown remarkPlugins={[remarkGfm]} skipHtml>
        {text}
      </ReactMarkdown>
    </div>
  );
}

function ThreadMessage() {
  return (
    <MessagePrimitive.Root className="flex w-full">
      <MessagePrimitive.If user>
        <div className="ml-auto max-w-[80%] rounded-lg bg-blue-600 px-4 py-3 text-white">
          <MessagePrimitive.Parts components={{ tools: { Fallback: ToolCall } }} />
        </div>
      </MessagePrimitive.If>
      <MessagePrimitive.If assistant>
        <div className="mr-auto max-w-[80%] rounded-lg bg-muted px-4 py-3 text-foreground">
          <MessagePrimitive.Parts
            components={{ Text: MarkdownText, tools: { Fallback: ToolCall } }}
          />
        </div>
      </MessagePrimitive.If>
    </MessagePrimitive.Root>
  );
}

function PlaygroundThread({
  messages,
  disabled,
  running,
  onNew,
}: {
  messages: ReturnType<typeof conversationToThread>["messages"];
  disabled: boolean;
  running: boolean;
  onNew: (message: AppendMessage) => Promise<void>;
}) {
  const runtime = useExternalStoreRuntime({
    messages,
    isDisabled: disabled,
    isRunning: running,
    onNew,
  });

  return (
    <AssistantRuntimeProvider runtime={runtime}>
      <ThreadPrimitive.Root className="flex min-h-0 flex-1 flex-col">
        <ThreadPrimitive.Viewport className="mb-4 flex-1 overflow-y-auto rounded-lg border border-border bg-card p-4 text-card-foreground shadow-sm">
          {messages.length === 0 && (
            <div className="flex h-full min-h-48 items-center justify-center text-muted-foreground">
              Start a conversation by choosing a published agent and sending a message.
            </div>
          )}
          <div className="space-y-4">
            <ThreadPrimitive.Messages components={{ Message: ThreadMessage }} />
          </div>
        </ThreadPrimitive.Viewport>

        <ComposerPrimitive.Root className="flex items-end gap-2">
          <ComposerPrimitive.Input
            aria-label="Message"
            placeholder="Type a message..."
            rows={2}
            className="min-h-12 flex-1 resize-none rounded-lg border border-border bg-card px-4 py-3 text-sm text-card-foreground disabled:cursor-not-allowed disabled:bg-muted"
          />
          <ComposerPrimitive.Send className="rounded-lg bg-blue-600 px-6 py-3 text-sm font-medium text-white hover:bg-blue-700 disabled:cursor-not-allowed disabled:opacity-50">
            Send message
          </ComposerPrimitive.Send>
        </ComposerPrimitive.Root>
      </ThreadPrimitive.Root>
    </AssistantRuntimeProvider>
  );
}

export default function PlaygroundPage() {
  const [agents, setAgents] = useState<Agent[]>([]);
  const [agentsLoaded, setAgentsLoaded] = useState(false);
  const [selectedAgentId, setSelectedAgentId] = useState("");
  const [conversationId, setConversationId] = useState<string | null>(null);
  const [turns, setTurns] = useState<Turn[]>([]);
  const [runActive, setRunActive] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const unsubscribeRef = useRef<(() => void) | null>(null);
  const restoredRef = useRef(false);

  const stopSubscription = useCallback(() => {
    unsubscribeRef.current?.();
    unsubscribeRef.current = null;
  }, []);

  const updateTurn = useCallback(
    (runId: string, update: (turn: Turn) => Turn) => {
      setTurns((current) =>
        current.map((turn) => (turn.runId === runId ? update(turn) : turn)),
      );
    },
    [],
  );

  // Follow one turn's run. Only the latest turn can be in flight, so one subscription is enough.
  const followRun = useCallback(
    (runId: string, initialLastSeq?: number) => {
      stopSubscription();
      const handlers: SubscribeHandlers = {
        onEvent: (nextEvent) => {
          updateTurn(runId, (turn) => {
            const events = turn.events.some((item) => item.seq === nextEvent.seq)
              ? turn.events
              : [...turn.events, nextEvent].sort((a, b) => a.seq - b.seq);
            let partials = turn.partials ?? {};
            let displayStatus = turn.displayStatus;
            if (nextEvent.type === "model_message") {
              partials = deltaReducer(partials, { type: "model_message", payload: nextEvent });
            } else if (nextEvent.type === "run_completed" || nextEvent.type === "run_failed") {
              partials = deltaReducer(partials, {
                type: nextEvent.type === "run_completed" ? "done" : "failed",
              });
              displayStatus =
                nextEvent.type === "run_completed"
                  ? "completed"
                  : failedEventStatus(nextEvent.data.status);
            }
            const error =
              nextEvent.type === "run_failed" && typeof nextEvent.data.error === "string"
                ? nextEvent.data.error
                : turn.error;
            return { ...turn, events, partials, displayStatus, error };
          });
          if (nextEvent.type === "run_completed" || nextEvent.type === "run_failed") {
            setRunActive(false);
          }
        },
        onDelta: (delta) => {
          updateTurn(runId, (turn) => ({
            ...turn,
            partials: deltaReducer(turn.partials ?? {}, { type: "delta", payload: delta }),
          }));
        },
        onStatus: (status) => {
          updateTurn(runId, (turn) => ({ ...turn, displayStatus: status.display_status }));
          setRunActive(!TERMINAL_STATUSES.has(status.display_status));
        },
        onEnd: (reason) => {
          if (
            reason === "run_completed" ||
            reason === "run_failed" ||
            reason === "terminal_status" ||
            reason === "not_found"
          ) {
            setRunActive(false);
          }
        },
      };
      unsubscribeRef.current =
        initialLastSeq === undefined
          ? subscribeRun(runId, handlers)
          : subscribeRun(runId, handlers, { initialLastSeq });
    },
    [stopSubscription, updateTurn],
  );

  useEffect(() => {
    let cancelled = false;
    void api.agents
      .list()
      .then((items) => {
        if (cancelled) return;
        setAgents(items);
        const firstPublished = items.find((agent) => agent.latest_version !== null);
        setSelectedAgentId(firstPublished?.id ?? "");
      })
      .catch(() => {
        if (!cancelled) setError("Failed to load agents.");
      })
      .finally(() => {
        if (!cancelled) setAgentsLoaded(true);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  // Reload: rebuild the whole thread from ?conversation=<id> and follow the in-flight turn, if any.
  useEffect(() => {
    if (!agentsLoaded || restoredRef.current) return;
    restoredRef.current = true;
    const id = new URLSearchParams(window.location.search).get("conversation");
    if (!id) return;

    let cancelled = false;
    setRunActive(true);
    void (async () => {
      try {
        const conversation = await getConversation(id);
        const loaded = await Promise.all(
          conversation.runs.map(async (run) => ({
            run,
            events: await listEvents(run.run_id),
          })),
        );
        if (cancelled) return;
        setConversationId(conversation.id);
        setSelectedAgentId(conversation.agent_id);
        setTurns(
          loaded.map(({ run, events }) => ({
            runId: run.run_id,
            input: run.input,
            events,
            partials: {},
            displayStatus: run.display_status,
            error: run.error,
          })),
        );
        setError(null);
        const latest = loaded[loaded.length - 1];
        if (!latest || TERMINAL_STATUSES.has(latest.run.display_status)) {
          setRunActive(false);
          return;
        }
        if (latest.run.status === "queued") {
          // Recorded but maybe never started (the start request was interrupted): retrying the
          // same turn is idempotent and enqueues it if it was not.
          await startTurn(conversation.id, latest.run.run_id, latest.run.input).catch(() => undefined);
          if (cancelled) return;
        }
        const lastSeq = latest.events.reduce((max, item) => Math.max(max, item.seq), -1);
        followRun(latest.run.run_id, lastSeq);
      } catch {
        if (!cancelled) {
          setRunActive(false);
          setError("Failed to load conversation.");
        }
      }
    })();

    return () => {
      cancelled = true;
    };
  }, [agentsLoaded, followRun]);

  useEffect(() => stopSubscription, [stopSubscription]);

  const resetConversation = useCallback(() => {
    stopSubscription();
    setConversationId(null);
    setTurns([]);
    setRunActive(false);
    setError(null);
    const url = new URL(window.location.href);
    url.searchParams.delete("conversation");
    window.history.replaceState({}, "", url);
  }, [stopSubscription]);

  const handleNewMessage = useCallback(
    async (message: AppendMessage) => {
      const input = messageText(message);
      if (!input || !selectedAgentId || runActive) return;

      const selectedAgent = agents.find((agent) => agent.id === selectedAgentId);
      if (!selectedAgent || selectedAgent.latest_version === null) return;

      const runId = crypto.randomUUID();
      setRunActive(true);
      setError(null);
      stopSubscription();
      setTurns((current) => [
        ...current,
        { runId, input, events: [], partials: {}, displayStatus: "queued", error: null },
      ]);

      try {
        let id = conversationId;
        if (id === null) {
          const created = await createConversation(
            selectedAgentId,
            input.slice(0, 80),
            crypto.randomUUID(),
          );
          id = created.id;
          setConversationId(id);
          const url = new URL(window.location.href);
          url.searchParams.set("conversation", id);
          window.history.replaceState({}, "", url);
        }
        const started = await startTurn(id, runId, input);
        updateTurn(runId, (turn) => ({ ...turn, displayStatus: started.display_status }));
        followRun(runId);
      } catch {
        setTurns((current) => current.filter((turn) => turn.runId !== runId));
        setRunActive(false);
        setError("Failed to send the message.");
      }
    },
    [agents, conversationId, followRun, runActive, selectedAgentId, stopSubscription, updateTurn],
  );

  const handleAgentChange = (agentId: string) => {
    setSelectedAgentId(agentId);
    resetConversation();
  };

  const selectedAgent = agents.find((agent) => agent.id === selectedAgentId);
  const composerDisabled =
    runActive || !selectedAgent || selectedAgent.latest_version === null;
  const thread = useMemo(() => conversationToThread(turns), [turns]);
  const latest = turns.length > 0 ? turns[turns.length - 1] : null;
  const status = latest?.displayStatus;
  const statusError =
    status === "needs_attention"
      ? latest?.error || "This run needs attention before it can continue."
      : status === "failed"
        ? latest?.error || "The run failed."
        : null;

  return (
    <div className="mx-auto flex h-[calc(100vh-8rem)] max-w-4xl flex-col">
        <div className="mb-6">
          <h1 className="mb-3 text-2xl font-bold">Playground</h1>
          <div className="flex flex-wrap items-center gap-4">
            <label className="flex items-center gap-2 text-sm font-medium text-muted-foreground">
              Agent
              <select
                aria-label="Agent"
                className="rounded-md border border-border bg-card px-3 py-2 text-sm text-card-foreground"
                value={selectedAgentId}
                onChange={(event) => handleAgentChange(event.target.value)}
              >
                {!agents.some((agent) => agent.latest_version !== null) && (
                  <option value="">No published agents</option>
                )}
                {agents.map((agent) => (
                  <option
                    key={agent.id}
                    value={agent.id}
                    disabled={agent.latest_version === null}
                  >
                    {agent.name}
                    {agent.latest_version === null && " — Publish a version first"}
                  </option>
                ))}
              </select>
            </label>

            {latest && status && (
              <div className="flex items-center gap-3">
                <StatusPill
                  status={status}
                  showRecoveringNotice={status === "recovering"}
                />
                <a
                  href={`/runs/${latest.runId}`}
                  className="text-sm text-blue-600 hover:underline"
                >
                  View run
                </a>
              </div>
            )}

            {turns.length > 0 && (
              <button
                type="button"
                onClick={resetConversation}
                disabled={runActive}
                className="ml-auto rounded-md border border-border px-3 py-2 text-sm hover:bg-muted disabled:cursor-not-allowed disabled:opacity-50"
              >
                New conversation
              </button>
            )}
          </div>

          {status === "recovering" && (
            <div className="mt-4 rounded-md border border-yellow-200 bg-yellow-50 p-3 text-sm text-yellow-700">
              The worker is restarting. Events may be delayed while the run recovers.
            </div>
          )}
          {(error || statusError) && (
            <div role="alert" className="mt-4 rounded-md border border-red-200 bg-red-50 p-3 text-sm text-red-700">
              {error || statusError}
            </div>
          )}
        </div>

        <PlaygroundThread
          messages={thread.messages}
          disabled={composerDisabled}
          running={runActive}
          onNew={handleNewMessage}
        />
    </div>
  );
}
