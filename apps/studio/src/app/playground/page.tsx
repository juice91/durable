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
import { runToThread } from "@/components/playground/thread";
import { StatusPill } from "@/components/runs/StatusPill";
import { api, type Agent } from "@/lib/api";
import {
  deltaReducer,
  getRun,
  listEvents,
  startRun,
  subscribeRun,
  type Run,
  type RunEvent,
  type RunStatus,
} from "@/lib/runs";

type PartialTextMap = Record<string, { text: string; attempt: string }>;
type ApiRun = Run & { run_id?: string };

const TERMINAL_STATUSES = new Set<RunStatus>([
  "completed",
  "failed",
  "needs_attention",
]);

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
    <div className="my-2 rounded-md border border-gray-200 bg-white p-3">
      <p className="text-sm font-semibold text-gray-800">{toolName}</p>
      <pre className="mt-2 overflow-x-auto text-xs text-gray-600">
        {JSON.stringify(args, null, 2)}
      </pre>
      {result !== undefined && (
        <pre className="mt-2 overflow-x-auto border-t border-gray-100 pt-2 text-xs text-gray-700">
          {JSON.stringify(result, null, 2)}
        </pre>
      )}
    </div>
  );
}

function MarkdownText({ text }: TextMessagePartProps) {
  return (
    <div className="space-y-2 break-words [&_a]:text-blue-600 [&_a]:underline [&_code]:rounded [&_code]:bg-gray-200 [&_code]:px-1 [&_pre]:overflow-x-auto [&_ul]:list-disc [&_ul]:pl-5">
      <ReactMarkdown remarkPlugins={[remarkGfm]} skipHtml>
        {text}
      </ReactMarkdown>
    </div>
  );
}

function normalizeRun(apiRun: Run, fallbackId: string): Run {
  const response = apiRun as ApiRun;
  return { ...response, id: response.id ?? response.run_id ?? fallbackId };
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
        <div className="mr-auto max-w-[80%] rounded-lg bg-gray-100 px-4 py-3 text-gray-900">
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
  messages: ReturnType<typeof runToThread>["messages"];
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
        <ThreadPrimitive.Viewport className="mb-4 flex-1 overflow-y-auto rounded-lg border bg-white p-4 shadow-sm">
          {messages.length === 0 && (
            <div className="flex h-full min-h-48 items-center justify-center text-gray-500">
              Start a run by choosing a published agent and sending a message.
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
            className="min-h-12 flex-1 resize-none rounded-lg border px-4 py-3 text-sm disabled:cursor-not-allowed disabled:bg-gray-100"
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
  const [run, setRun] = useState<Run | null>(null);
  const [events, setEvents] = useState<RunEvent[]>([]);
  const [partials, setPartials] = useState<PartialTextMap>({});
  const [runActive, setRunActive] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const unsubscribeRef = useRef<(() => void) | null>(null);
  const restoredRunRef = useRef(false);

  const stopSubscription = useCallback(() => {
    unsubscribeRef.current?.();
    unsubscribeRef.current = null;
  }, []);

  const followRun = useCallback(
    (runId: string) => {
      stopSubscription();
      unsubscribeRef.current = subscribeRun(runId, {
        onEvent: (nextEvent) => {
          setEvents((current) => {
            if (current.some((item) => item.seq === nextEvent.seq)) return current;
            return [...current, nextEvent].sort((a, b) => a.seq - b.seq);
          });

          if (nextEvent.type === "model_message") {
            setPartials((current) =>
              deltaReducer(current, {
                type: "model_message",
                payload: nextEvent,
              }),
            );
          } else if (
            nextEvent.type === "run_completed" ||
            nextEvent.type === "run_failed"
          ) {
            setPartials((current) =>
              deltaReducer(current, {
                type: nextEvent.type === "run_completed" ? "done" : "failed",
              }),
            );
            setRunActive(false);
            setRun((current) =>
              current
                ? {
                    ...current,
                    display_status:
                      nextEvent.type === "run_completed" ? "completed" : "failed",
                  }
                : current,
            );
          }
        },
        onDelta: (delta) => {
          setPartials((current) =>
            deltaReducer(current, { type: "delta", payload: delta }),
          );
        },
        onStatus: (status) => {
          setRun((current) =>
            current
              ? { ...current, display_status: status.display_status }
              : current,
          );
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
      });
    },
    [stopSubscription],
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

  useEffect(() => {
    if (!agentsLoaded || restoredRunRef.current) return;
    restoredRunRef.current = true;
    const runId = new URLSearchParams(window.location.search).get("run");
    if (!runId) return;

    let cancelled = false;
    void Promise.all([getRun(runId), listEvents(runId)])
      .then(([loadedRun, loadedEvents]) => {
        if (cancelled) return;
        setRun(normalizeRun(loadedRun, runId));
        setSelectedAgentId(loadedRun.agent_id);
        setEvents(loadedEvents);
        setPartials({});
        setRunActive(!TERMINAL_STATUSES.has(loadedRun.display_status));
        setError(null);
        followRun(runId);
      })
      .catch(() => {
        if (!cancelled) {
          setRunActive(false);
          setError("Failed to load run.");
        }
      });

    return () => {
      cancelled = true;
    };
  }, [agentsLoaded, followRun]);

  useEffect(() => stopSubscription, [stopSubscription]);

  const handleNewMessage = useCallback(
    async (message: AppendMessage) => {
      const input = messageText(message);
      if (!input || !selectedAgentId || runActive) return;

      const selectedAgent = agents.find((agent) => agent.id === selectedAgentId);
      if (!selectedAgent || selectedAgent.latest_version === null) return;

      const runId = crypto.randomUUID();
      setRunActive(true);
      setRun(null);
      setEvents([]);
      setPartials({});
      setError(null);
      stopSubscription();

      try {
        const startedRun = await startRun(selectedAgentId, runId, input, null);
        setRun(normalizeRun(startedRun, runId));
        const url = new URL(window.location.href);
        url.searchParams.set("run", runId);
        window.history.replaceState({}, "", url);
        followRun(runId);
      } catch {
        setRunActive(false);
        setError("Failed to start run.");
      }
    },
    [agents, followRun, runActive, selectedAgentId, stopSubscription],
  );

  const handleAgentChange = (agentId: string) => {
    stopSubscription();
    setSelectedAgentId(agentId);
    setRun(null);
    setEvents([]);
    setPartials({});
    setRunActive(false);
    setError(null);
    const url = new URL(window.location.href);
    url.searchParams.delete("run");
    window.history.replaceState({}, "", url);
  };

  const selectedAgent = agents.find((agent) => agent.id === selectedAgentId);
  const composerDisabled =
    runActive || !selectedAgent || selectedAgent.latest_version === null;
  const thread = useMemo(() => runToThread(events, partials), [events, partials]);
  const status = run?.display_status;
  const statusError =
    status === "needs_attention"
      ? run?.error || "This run needs attention before it can continue."
      : status === "failed"
        ? run?.error || "The run failed."
        : null;

  return (
    <div className="mx-auto flex h-[calc(100vh-8rem)] max-w-4xl flex-col">
        <div className="mb-6">
          <h1 className="mb-3 text-2xl font-bold">Playground</h1>
          <div className="flex flex-wrap items-center gap-4">
            <label className="flex items-center gap-2 text-sm font-medium text-gray-700">
              Agent
              <select
                aria-label="Agent"
                className="rounded-md border px-3 py-2 text-sm"
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

            {run && status && (
              <div className="flex items-center gap-3">
                <StatusPill
                  status={status}
                  showRecoveringNotice={status === "recovering"}
                />
                <a
                  href={`/runs/${run.id}`}
                  className="text-sm text-blue-600 hover:underline"
                >
                  View run
                </a>
              </div>
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
        <p className="mt-2 text-center text-xs text-gray-500">
          Each message is one run; conversation memory is not available in this milestone.
        </p>
    </div>
  );
}
