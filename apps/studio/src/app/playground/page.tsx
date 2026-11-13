"use client";

import React, {
  createContext,
  useCallback,
  useContext,
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
  useAuiState,
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
import { ConversationRail } from "@/components/playground/ConversationRail";
import { ContextPanel } from "@/components/playground/ContextPanel";
import { RunOptionsBar } from "@/components/playground/RunOptionsBar";
import { TurnFooter, type TurnView } from "@/components/playground/TurnFooter";
import {
  DEFAULT_MODEL_CHOICE,
  NO_OPTION_ERRORS,
  choiceFromModelId,
  classifyTurnError,
  contextToSend,
  effectiveVersionRef,
  hasOutputProcessors,
  clearPlaygroundContext,
  loadPlaygroundContext,
  optionsFromRun,
  overrideFor,
  parseUrlState,
  replaceUrlState,
  resolveModelChoice,
  savePlaygroundContext,
  stripPreferPresetParam,
  specModelId,
  targetSpec,
  turnBlock,
  turnLiveText,
  turnModelId,
  turnUsage,
  type ModelChoice,
  type RunUsageRow,
  type TurnOptionErrors,
  type TurnOptions,
  type VersionRef,
} from "@/components/playground/runOptions";
import { TurnStartError, startTurnWithOptions } from "@/components/playground/turnApi";
import { IDLE_VALIDATION, type ContextValidationState } from "@/components/requestContext";
import { StatusPill } from "@/components/runs/StatusPill";
import { api, type Agent, type AgentVersion } from "@/lib/api";
import {
  createConversation,
  getConversation,
  type Conversation,
} from "@/lib/conversations";
import { fetchModels, type ModelOption } from "@/lib/metrics";
import {
  getRequestContext,
  loadSavedContext,
  saveContext,
  type RequestContextInfo,
  type RequestContextValue,
} from "@/lib/requestContext";
import {
  deltaReducer,
  listEvents,
  subscribeRun,
  type RunStatus,
  type SubscribeHandlers,
} from "@/lib/runs";

/** One turn of the conversation: a run plus what the thread and its badge need to draw it. */
type Turn = ThreadTurn & {
  displayStatus: RunStatus;
  error: string | null;
  /** The effective model, from the start response or the run row (null until known). */
  modelId: string | null;
  modelOverridden: boolean;
  draft: boolean;
  version: number | null;
  /** The run row's usage totals, when the turn was loaded from the API. */
  row: RunUsageRow | null;
  /**
   * Whether live deltas may be shown, fixed when the turn starts: false when the target spec has
   * output processors (P0.17). null: not known at start (spec not loaded yet, or a turn rebuilt on
   * reload), worked out from the turn's own version or draft, failing closed while unknown.
   */
  liveText: boolean | null;
};

const TERMINAL_STATUSES = new Set<RunStatus>([
  "completed",
  "failed",
  "needs_attention",
]);

/** Thread message id → the turn whose badge and notices render under it (its last message). */
const TurnViewsContext = createContext<ReadonlyMap<string, TurnView>>(new Map());

function failedEventStatus(status: unknown): "failed" | "needs_attention" {
  return status === "needs_attention" ? "needs_attention" : "failed";
}

function setConversationParam(id: string | null) {
  replaceUrlState({ conversation: id });
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
  const messageId = useAuiState((s) => s.message.id);
  const turn = useContext(TurnViewsContext).get(messageId);
  return (
    <MessagePrimitive.Root className="flex w-full flex-col">
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
      {turn && <TurnFooter turn={turn} />}
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
              Start a conversation by choosing an agent and sending a message.
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

/** Thread message id of each turn's last message, keyed by that id (message ids are `<runId>:...`). */
function footerMessageIds(messages: readonly { id: string }[]): Map<string, string> {
  const lastByRun = new Map<string, string>();
  for (const message of messages) {
    const colon = message.id.indexOf(":");
    if (colon > 0) lastByRun.set(message.id.slice(0, colon), message.id);
  }
  return new Map(Array.from(lastByRun, ([runId, messageId]) => [messageId, runId]));
}

export default function PlaygroundPage() {
  const [agents, setAgents] = useState<Agent[]>([]);
  const [agentsLoaded, setAgentsLoaded] = useState(false);
  const [selectedAgentId, setSelectedAgentId] = useState("");
  const [conversationId, setConversationId] = useState<string | null>(null);
  const [turns, setTurns] = useState<Turn[]>([]);
  const [runActive, setRunActive] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [railRefresh, setRailRefresh] = useState(0);
  const unsubscribeRef = useRef<(() => void) | null>(null);
  const restoredRef = useRef(false);
  // The conversation named by the URL at load time (read once, on the client).
  const initialConversationRef = useRef<string | null>(null);
  // Bumped whenever the thread switches (select, new conversation, agent change): a load or a turn
  // start begun for the previous thread must not write into the new one or subscribe.
  const generationRef = useRef(0);
  const agentRef = useRef("");
  // What onDelta needs to decide whether a turn may show unchecked text (see Turn.liveText).
  const liveTextRef = useRef<{ agent: Agent | undefined; versions: AgentVersion[] | null }>({
    agent: undefined,
    versions: null,
  });
  // The turn that became blocked while this page watched it: only its notice is announced.
  const [announceRunId, setAnnounceRunId] = useState<string | null>(null);

  // Run options (P0.19): per turn, version or draft, model override and request context.
  const [versions, setVersions] = useState<AgentVersion[] | null>(null);
  const [requestedVersion, setRequestedVersion] = useState<VersionRef>(null);
  const [models, setModels] = useState<ModelOption[]>([]);
  const [modelsLoaded, setModelsLoaded] = useState(false);
  const [modelsError, setModelsError] = useState<string | null>(null);
  const [modelChoice, setModelChoice] = useState<ModelChoice>(DEFAULT_MODEL_CHOICE);
  // A model override from the URL, applied once the model list and the target spec are known.
  const [pendingModelId, setPendingModelId] = useState<string | null>(null);
  const [modelNotice, setModelNotice] = useState<string | null>(null);
  const [contextOpen, setContextOpen] = useState(false);
  const [contextInfo, setContextInfo] = useState<RequestContextInfo | null>(null);
  const [contextLoading, setContextLoading] = useState(false);
  const [contextLoadError, setContextLoadError] = useState<string | null>(null);
  const [contextValue, setContextValue] = useState<RequestContextValue>({});
  const [contextValidation, setContextValidation] = useState<ContextValidationState>(IDLE_VALIDATION);
  const [presetAvailable, setPresetAvailable] = useState(false);
  const [presetMessage, setPresetMessage] = useState<string | null>(null);
  const [optionErrors, setOptionErrors] = useState<TurnOptionErrors>(NO_OPTION_ERRORS);
  const [unsentInput, setUnsentInput] = useState<string | null>(null);
  // The URL is written only after it has been read, so a reload never loses its options.
  const [urlReady, setUrlReady] = useState(false);

  const selectedAgent = agents.find((agent) => agent.id === selectedAgentId);
  const latestVersion = selectedAgent?.latest_version ?? null;
  const versionRef = selectedAgent
    ? effectiveVersionRef(requestedVersion, latestVersion, versions)
    : requestedVersion;
  const spec = selectedAgent ? targetSpec(versionRef, selectedAgent.draft_spec, latestVersion, versions) : null;
  const defaultModelId = specModelId(spec);
  const resolvedModel = resolveModelChoice(modelChoice);
  const chosenModelId = "modelId" in resolvedModel ? resolvedModel.modelId : null;
  const modelChoiceError = "error" in resolvedModel ? resolvedModel.error : null;

  const refreshRail = useCallback(() => setRailRefresh((n) => n + 1), []);

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

  /** Select an agent: its remembered playground context, else its saved preset (P0.16). */
  const selectAgent = useCallback((agentId: string, preferPreset = false) => {
    agentRef.current = agentId;
    setSelectedAgentId(agentId);
    const preset = agentId ? loadSavedContext(agentId) : null;
    // "Use in playground" (`context=preset`): the saved preset replaces the tab's remembered context.
    if (preferPreset && preset !== null) clearPlaygroundContext(agentId);
    const remembered = agentId ? loadPlaygroundContext(agentId) : null;
    setContextValue(remembered ?? preset?.value ?? {});
    setPresetAvailable(preset !== null);
    setPresetMessage(remembered === null && preset !== null ? "Using the preset saved for this agent." : null);
    setContextValidation(IDLE_VALIDATION);
    setOptionErrors(NO_OPTION_ERRORS);
    setUnsentInput(null);
  }, []);

  const resetOptions = useCallback(() => {
    setRequestedVersion(null);
    setModelChoice(DEFAULT_MODEL_CHOICE);
    setPendingModelId(null);
    setModelNotice(null);
  }, []);

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
            const modelId =
              turn.modelId ??
              (nextEvent.type === "model_message" && typeof nextEvent.data.model_id === "string"
                ? nextEvent.data.model_id
                : null);
            return { ...turn, events, partials, displayStatus, error, modelId };
          });
          if (nextEvent.type === "run_failed" && turnBlock([nextEvent]) !== null) {
            setAnnounceRunId(runId);
          }
          if (nextEvent.type === "run_completed" || nextEvent.type === "run_failed") {
            setRunActive(false);
            refreshRail();
          }
        },
        onDelta: (delta) => {
          updateTurn(runId, (turn) => {
            // Defence in depth for P0.17: the worker publishes no deltas for a spec with output
            // processors; should any arrive anyway, unchecked text never reaches the screen.
            const { agent, versions: known } = liveTextRef.current;
            if (!turnLiveText(turn.liveText, turn, agent?.draft_spec, agent?.latest_version ?? null, known)) {
              return turn;
            }
            return {
              ...turn,
              partials: deltaReducer(turn.partials ?? {}, { type: "delta", payload: delta }),
            };
          });
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
    [refreshRail, stopSubscription, updateTurn],
  );

  // Agents, then the options the URL names (`?agent=&version=&model=`; `agent` also comes from
  // the Request Context page's "Use in playground").
  useEffect(() => {
    let cancelled = false;
    void api.agents
      .list()
      .then((items) => {
        if (cancelled) return;
        setAgents(items);
        const initial = parseUrlState(window.location.search);
        initialConversationRef.current = initial.conversation;
        const fromUrl = initial.agent ? items.find((agent) => agent.id === initial.agent) : undefined;
        const chosen = fromUrl ?? items.find((agent) => agent.latest_version !== null) ?? items[0];
        selectAgent(chosen?.id ?? "", Boolean(fromUrl) && initial.preferPreset);
        stripPreferPresetParam();
        if (fromUrl || !initial.agent) {
          setRequestedVersion(initial.version);
          setPendingModelId(initial.model);
        }
      })
      .catch(() => {
        if (!cancelled) setError("Failed to load agents.");
      })
      .finally(() => {
        if (cancelled) return;
        setAgentsLoaded(true);
        setUrlReady(true);
      });
    return () => {
      cancelled = true;
    };
  }, [selectAgent]);

  // The model list (allowlist plus scripted test models), once.
  useEffect(() => {
    let cancelled = false;
    fetchModels()
      .then((items) => {
        if (!cancelled) setModels(items);
      })
      .catch(() => {
        if (!cancelled) setModelsError("Could not load the model list; turns use the version's model.");
      })
      .finally(() => {
        if (!cancelled) setModelsLoaded(true);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  // The selected agent's published versions (the version selector and each turn's spec).
  useEffect(() => {
    let cancelled = false;
    setVersions(null);
    if (selectedAgentId) {
      api.versions
        .list(selectedAgentId)
        .then((items) => {
          if (!cancelled) setVersions(Array.isArray(items) ? items : []);
        })
        .catch(() => {
          if (!cancelled) setVersions([]);
        });
    }
    return () => {
      cancelled = true;
    };
  }, [selectedAgentId]);

  // The target's context fields.
  const contextVersionKey = String(versionRef);
  useEffect(() => {
    let cancelled = false;
    setContextInfo(null);
    setContextLoadError(null);
    if (!selectedAgentId) {
      setContextLoading(false);
      return;
    }
    setContextLoading(true);
    const ref: VersionRef = contextVersionKey === "draft" ? "draft" : contextVersionKey === "null" ? null : Number(contextVersionKey);
    getRequestContext(selectedAgentId, ref)
      .then((info) => {
        if (!cancelled) setContextInfo(info);
      })
      .catch(() => {
        if (!cancelled) setContextLoadError("Could not load this version's context fields.");
      })
      .finally(() => {
        if (!cancelled) setContextLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [selectedAgentId, contextVersionKey]);

  // Apply a model override named by the URL once it can be checked against the list.
  const specKnown = spec !== null || versions !== null;
  useEffect(() => {
    if (pendingModelId === null || !modelsLoaded || !specKnown) return;
    const { choice, dropped } = choiceFromModelId(pendingModelId, models, defaultModelId);
    setModelChoice(choice);
    setModelNotice(dropped ? `${pendingModelId} is not offered here; turns use the version's model.` : null);
    setPendingModelId(null);
  }, [defaultModelId, models, modelsLoaded, pendingModelId, specKnown]);

  // Keep the options in the URL so a reload restores them.
  const urlModel = pendingModelId ?? chosenModelId;
  useEffect(() => {
    if (!urlReady) return;
    replaceUrlState({
      agent: selectedAgentId || null,
      version: versionRef,
      model: urlModel,
    });
  }, [selectedAgentId, urlModel, urlReady, versionRef]);

  useEffect(() => {
    agentRef.current = selectedAgentId;
  }, [selectedAgentId]);

  useEffect(() => {
    liveTextRef.current = { agent: selectedAgent, versions };
  }, [selectedAgent, versions]);

  // Rebuild a whole thread from the API and follow its in-flight turn, if any. Used on reload
  // (?conversation=<id>) and when a conversation is picked in the rail. Leaving a thread whose turn
  // is streaming only stops this page's subscription: the run goes on server-side and is followed
  // again when the thread is opened again.
  const loadConversation = useCallback(
    async (id: string) => {
      stopSubscription();
      const generation = ++generationRef.current;
      const current = () => generation === generationRef.current;
      setConversationId(id);
      setTurns([]);
      setError(null);
      setAnnounceRunId(null);
      setRunActive(true);
      try {
        const conversation = await getConversation(id);
        const loaded = await Promise.all(
          conversation.runs.map(async (run) => ({
            run,
            events: await listEvents(run.run_id),
          })),
        );
        if (!current()) return;
        setConversationId(conversation.id);
        if (conversation.agent_id !== agentRef.current) {
          // Another agent's conversation (an old link): its own defaults, not the URL's options.
          selectAgent(conversation.agent_id);
          resetOptions();
        }
        setTurns(
          loaded.map(({ run, events }) => {
            const row = run as typeof run & RunUsageRow;
            return {
              runId: run.run_id,
              input: run.input,
              events,
              partials: {},
              displayStatus: run.display_status,
              error: run.error,
              modelId: turnModelId(events, row),
              modelOverridden: Boolean(row.model_overridden),
              draft: Boolean(row.draft),
              version: run.version,
              row,
              liveText: null,
            };
          }),
        );
        const latest = loaded[loaded.length - 1];
        if (!latest || TERMINAL_STATUSES.has(latest.run.display_status)) {
          setRunActive(false);
          return;
        }
        if (latest.run.status === "queued") {
          // Recorded but maybe never started (the start request was interrupted): retrying the
          // same turn with the options it was recorded with is idempotent and enqueues it if it
          // was not.
          await startTurnWithOptions(
            conversation.id,
            latest.run.run_id,
            latest.run.input,
            optionsFromRun(latest.run as typeof latest.run & RunUsageRow),
          ).catch(() => undefined);
          if (!current()) return;
        }
        const lastSeq = latest.events.reduce((max, item) => Math.max(max, item.seq), -1);
        followRun(latest.run.run_id, lastSeq);
      } catch {
        if (current()) {
          setRunActive(false);
          setError("Failed to load conversation.");
        }
      }
    },
    [followRun, resetOptions, selectAgent, stopSubscription],
  );

  // Reload: rebuild the thread named by ?conversation=<id>.
  useEffect(() => {
    if (!agentsLoaded || restoredRef.current) return;
    restoredRef.current = true;
    const id = initialConversationRef.current ?? new URLSearchParams(window.location.search).get("conversation");
    if (id) void loadConversation(id);
  }, [agentsLoaded, loadConversation]);

  // Unmounting drops any pending load or turn start, as a thread switch does.
  useEffect(
    () => () => {
      generationRef.current += 1;
    },
    [],
  );

  useEffect(() => stopSubscription, [stopSubscription]);

  const resetConversation = useCallback(() => {
    stopSubscription();
    generationRef.current += 1;
    setConversationId(null);
    setTurns([]);
    setRunActive(false);
    setError(null);
    setAnnounceRunId(null);
    setConversationParam(null);
  }, [stopSubscription]);

  const handleSelectConversation = useCallback(
    (conversation: Conversation) => {
      if (conversation.id === conversationId) return;
      setConversationParam(conversation.id);
      void loadConversation(conversation.id);
    },
    [conversationId, loadConversation],
  );

  const handleNewMessage = useCallback(
    async (message: AppendMessage) => {
      const input = messageText(message);
      if (!input || !selectedAgentId || runActive) return;

      const agent = agents.find((item) => item.id === selectedAgentId);
      if (!agent) return;
      if (modelChoiceError) {
        setOptionErrors((errors) => ({ ...errors, model: modelChoiceError }));
        setUnsentInput(input);
        return;
      }

      const options: TurnOptions = {
        version: versionRef,
        model: overrideFor(chosenModelId, defaultModelId),
        // A target without context fields gets none (a remembered value may hold stale keys).
        request_context: contextInfo !== null && contextInfo.fields.length === 0 ? null : contextToSend(contextValue),
      };
      const runId = crypto.randomUUID();
      const generation = generationRef.current;
      const current = () => generation === generationRef.current;
      setRunActive(true);
      setError(null);
      setOptionErrors(NO_OPTION_ERRORS);
      setUnsentInput(null);
      setAnnounceRunId(null);
      stopSubscription();
      // Known only when the target spec is: the draft's from the agent, a version's from the list.
      const targetKnown = spec !== null && (options.version === "draft" || versions !== null);
      setTurns((existing) => [
        ...existing,
        {
          runId,
          input,
          events: [],
          partials: {},
          displayStatus: "queued",
          error: null,
          modelId: options.model ? chosenModelId : defaultModelId,
          modelOverridden: options.model !== null,
          draft: options.version === "draft",
          version: typeof options.version === "number" ? options.version : options.version === null ? agent.latest_version : null,
          row: null,
          liveText: targetKnown ? !hasOutputProcessors(spec) : null,
        },
      ]);

      try {
        let id = conversationId;
        if (id === null) {
          // No title: the API titles the conversation from its first turn.
          const created = await createConversation(selectedAgentId, null, crypto.randomUUID());
          id = created.id;
          // The send is committed once the conversation exists: if the user switched threads
          // meanwhile, the turn is still started below (no empty conversation is left behind),
          // only this page no longer shows it.
          if (current()) {
            setConversationId(id);
            setConversationParam(id);
          }
        }
        const started = await startTurnWithOptions(id, runId, input, options);
        refreshRail();
        // The user switched threads meanwhile: the turn is recorded and runs; it shows when its
        // conversation is opened again.
        if (!current()) return;
        updateTurn(runId, (turn) => ({
          ...turn,
          displayStatus: started.display_status,
          modelId: started.model_id ?? turn.modelId,
          modelOverridden: started.model_overridden ?? turn.modelOverridden,
          draft: started.draft ?? turn.draft,
          version: started.draft ? null : (started.version ?? turn.version),
        }));
        followRun(runId);
      } catch (reason) {
        if (!current()) return;
        setTurns((existing) => existing.filter((turn) => turn.runId !== runId));
        setRunActive(false);
        if (reason instanceof TurnStartError && reason.status === 422) {
          // Shown next to the control the refusal is about.
          const errors = classifyTurnError(reason.status, reason.detail, options);
          setOptionErrors(errors);
          setUnsentInput(input);
          if (errors.context) setContextOpen(true);
        } else if (reason instanceof TurnStartError) {
          setError(`Failed to send the message: ${reason.message}`);
        } else {
          setError("Failed to send the message.");
        }
      }
    },
    [
      agents,
      chosenModelId,
      contextInfo,
      contextValue,
      conversationId,
      defaultModelId,
      followRun,
      modelChoiceError,
      refreshRail,
      runActive,
      selectedAgentId,
      spec,
      stopSubscription,
      updateTurn,
      versionRef,
      versions,
    ],
  );

  const handleAgentChange = (agentId: string) => {
    selectAgent(agentId);
    resetOptions();
    resetConversation();
  };

  const handleVersionChange = (ref: VersionRef) => {
    setRequestedVersion(ref);
    setOptionErrors((errors) => ({ ...errors, version: null, context: null, general: null }));
  };

  const handleModelChoiceChange = (choice: ModelChoice) => {
    setModelChoice(choice);
    setModelNotice(null);
    setOptionErrors((errors) => ({ ...errors, model: null, general: null }));
  };

  const handleContextChange = (value: RequestContextValue) => {
    setContextValue(value);
    setPresetMessage(null);
    setOptionErrors((errors) => ({ ...errors, context: null }));
    if (selectedAgentId) savePlaygroundContext(selectedAgentId, value);
  };

  const handleLoadPreset = () => {
    const preset = selectedAgentId ? loadSavedContext(selectedAgentId) : null;
    if (!preset) {
      setPresetAvailable(false);
      setPresetMessage("No preset is saved for this agent.");
      return;
    }
    handleContextChange(preset.value);
    setPresetMessage("Loaded the preset saved for this agent.");
  };

  const handleSavePreset = () => {
    if (!selectedAgentId || contextValidation.status !== "valid" || !contextValidation.accepted) return;
    const saved = saveContext(selectedAgentId, contextValidation.accepted, versionRef);
    setPresetAvailable(saved !== null || presetAvailable);
    setPresetMessage(saved ? "Saved as this agent's preset." : "Browser storage is unavailable; the preset was not saved.");
  };

  const handleClearContext = () => {
    handleContextChange({});
  };

  const composerDisabled = runActive || !selectedAgent || Boolean(modelChoiceError);
  const thread = useMemo(() => conversationToThread(turns), [turns]);

  const turnViews = useMemo(() => {
    const byRun = new Map(turns.map((turn) => [turn.runId, turn]));
    const views = new Map<string, TurnView>();
    for (const [messageId, runId] of footerMessageIds(thread.messages)) {
      const turn = byRun.get(runId);
      if (!turn) continue;
      const running = !TERMINAL_STATUSES.has(turn.displayStatus);
      const liveText = turnLiveText(
        turn.liveText,
        turn,
        selectedAgent?.draft_spec,
        selectedAgent?.latest_version ?? null,
        versions,
      );
      const block = turnBlock(turn.events);
      views.set(messageId, {
        runId,
        status: turn.displayStatus,
        modelId: turn.modelId ?? turnModelId(turn.events, turn.row),
        modelOverridden: turn.modelOverridden,
        draft: turn.draft,
        version: turn.version,
        usage: turnUsage(turn.events, turn.row),
        // Text is withheld (processors, or the spec not known yet): say why nothing streams.
        checking: running && !liveText,
        liveText,
        block,
        announce: block !== null && announceRunId === runId,
      });
    }
    return views;
  }, [announceRunId, selectedAgent, thread.messages, turns, versions]);

  const latest = turns.length > 0 ? turns[turns.length - 1] : null;
  const status = latest?.displayStatus;
  // A blocked turn explains itself under its message; other failures show here.
  const latestBlocked = latest ? turnBlock(latest.events) !== null : false;
  const statusError =
    status === "needs_attention"
      ? latest?.error || "This run needs attention before it can continue."
      : status === "failed" && !latestBlocked
        ? latest?.error || "The run failed."
        : null;
  const contextFieldCount = contextInfo ? contextInfo.fields.length : null;
  const contextValueCount = contextInfo
    ? Object.keys(contextToSend(contextValue) ?? {}).filter((key) => contextInfo.fields.some((f) => f.name === key)).length
    : 0;
  const contextHasErrors = Boolean(optionErrors.context) || contextValidation.status === "invalid";
  const optionsRefused = Boolean(optionErrors.context || optionErrors.model || optionErrors.version || optionErrors.general);

  return (
    <div className="mx-auto flex h-[calc(100vh-8rem)] max-w-6xl flex-col gap-4 md:flex-row">
      <div className="flex max-h-56 min-h-0 md:max-h-none">
        <ConversationRail
          agentId={selectedAgent ? selectedAgentId : ""}
          selectedId={conversationId}
          refreshKey={railRefresh}
          onSelect={handleSelectConversation}
          onNew={resetConversation}
        />
      </div>
      <div className="flex min-h-0 min-w-0 flex-1 flex-col">
        <div className="mb-6">
          <div className="mb-3 flex flex-wrap items-center gap-3">
            <h1 className="text-2xl font-bold">Playground</h1>
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
          </div>

          <RunOptionsBar
            agents={agents}
            selectedAgentId={selectedAgentId}
            onAgentChange={handleAgentChange}
            versions={versions}
            latestVersion={latestVersion}
            versionRef={versionRef}
            onVersionChange={handleVersionChange}
            versionError={optionErrors.version}
            defaultModelId={defaultModelId}
            models={models}
            modelsError={modelsError}
            modelChoice={modelChoice}
            onModelChoiceChange={handleModelChoiceChange}
            modelError={optionErrors.model ?? modelChoiceError}
            modelNotice={modelNotice}
            contextOpen={contextOpen}
            onToggleContext={() => setContextOpen((open) => !open)}
            contextFieldCount={contextFieldCount}
            contextValueCount={contextValueCount}
            contextHasErrors={contextHasErrors}
          />

          {contextOpen && selectedAgentId && (
            <ContextPanel
              agentId={selectedAgentId}
              versionRef={versionRef}
              info={contextInfo}
              loading={contextLoading}
              loadError={contextLoadError}
              value={contextValue}
              onChange={handleContextChange}
              onValidation={setContextValidation}
              turnErrors={optionErrors.context}
              presetAvailable={presetAvailable}
              onLoadPreset={handleLoadPreset}
              onSavePreset={handleSavePreset}
              canSavePreset={contextValidation.status === "valid" && contextValidation.accepted !== null}
              presetMessage={presetMessage}
              onClear={handleClearContext}
            />
          )}

          {optionErrors.general && (
            <div role="alert" data-testid="turn-options-error" className="mt-4 rounded-md border border-destructive/30 bg-destructive/10 p-3 text-sm text-destructive-text">
              {optionErrors.general}
            </div>
          )}
          {optionsRefused && unsentInput && (
            <p data-testid="unsent-message" className="mt-2 text-xs text-muted-foreground">
              Not sent: <span className="font-mono">{unsentInput.length > 120 ? `${unsentInput.slice(0, 120)}…` : unsentInput}</span>
            </p>
          )}

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

        <TurnViewsContext.Provider value={turnViews}>
          <PlaygroundThread
            messages={thread.messages}
            disabled={composerDisabled}
            running={runActive}
            onNew={handleNewMessage}
          />
        </TurnViewsContext.Provider>
      </div>
    </div>
  );
}
