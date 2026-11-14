"use client";

import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { AppendMessage } from "@assistant-ui/react";
import Link from "next/link";
import {
  conversationToThread,
  type ThreadTurn,
} from "@/components/chat/thread";
import type { TurnView } from "@/components/chat/TurnFooter";
import { toolCallInfoKey, toolCallInfos, type ToolCallInfo } from "@/components/chat/toolCalls";
import {
  DEFAULT_MODEL_CHOICE,
  NO_OPTION_ERRORS,
  choiceFromModelId,
  classifyTurnError,
  contextToSend,
  draftDiffersFromLatest,
  effectiveVersionRef,
  hasOutputProcessors,
  clearPlaygroundContext,
  loadPlaygroundContext,
  optionsFromRun,
  overrideFor,
  parseUrlState,
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
} from "@/components/chat/runOptions";
import { TurnStartError, startTurnWithOptions } from "@/components/chat/turnApi";
import { IDLE_VALIDATION, type ContextValidationState } from "@/components/requestContext";
import { RequestContextPopover } from "@/components/requestContext/RequestContextPopover";
import { StatusPill } from "@/components/runs/StatusPill";
import { TraceLink } from "@/components/traces/TraceLink";
import { api, ApiError, type Agent, type AgentVersion } from "@/lib/api";
import {
  createConversation,
  deleteConversation,
  getConversation,
  type Conversation,
} from "@/lib/conversations";
import { fetchModels, type ModelOption } from "@/lib/metrics";
import {
  RequestContextValidationError,
  getRequestContext,
  latestPreset,
  validateRequestContext,
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
import { usePathname } from "next/navigation";
import { agentChatPathState, routes } from "@/lib/routes";
import { matchesNow, WIDE_QUERY } from "@/components/frame/useMediaQuery";
import { useAgentPage } from "./AgentPageContext";
import { AgentChatPanes } from "./AgentChatPanes";
import { ChatPane, footerMessageIds } from "./ChatPane";
import { Composer } from "./Composer";
import { ComposerFieldError } from "./ComposerChip";
import { ComposerModelChip, MODEL_ERROR_ID } from "./ComposerModelChip";
import { ComposerVersionChip, VERSION_ERROR_ID } from "./ComposerVersionChip";
import { ThreadsPanel } from "./ThreadsPanel";
import { ComposerActAsChip } from "./ComposerActAsChip";
import { chatUrlFor, effectiveConfig, writeLastAgent, type ChatUrlState } from "./agentPageModel";
import { memoryOf, OBSERVATIONAL_DEFAULTS } from "@/components/agents/knowledgeMemory";
import { agentHasMemory, latestPublishedSpec, readActAs, writeActAs } from "@/components/memory/memoryModel";
import { useMemoryActivity, type LatestTurn } from "@/components/memory/useMemoryActivity";
import { draftDirty, targetKeyOf, type WorkingMemoryDraft, type WorkingMemoryDraftStore } from "@/components/memory/WorkingMemoryEditor";
import { inspectorTarget } from "@/components/memory/MemoryInspector";
import { primeWorkingMemory } from "@/lib/workingMemoryCache";
import { WorkingMemoryGuard } from "@/components/memory/WorkingMemoryGuard";
import { WORKING_MEMORY_GUARD_COPY } from "@/components/memory/memoryModel";
import { ConfirmDialog } from "@/components/shared/ConfirmDialog";

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

function failedEventStatus(status: unknown): "failed" | "needs_attention" {
  return status === "needs_attention" ? "needs_attention" : "failed";
}

/**
 * Deletes a conversation this tab created for a first send that was refused, if it still has no
 * turns (a turn start that failed in transit may have been recorded: then it is kept). Best effort:
 * a 404 (already gone) or any failure is ignored, and nothing is shown.
 */
export async function discardEmptyConversation(conversationId: string): Promise<void> {
  try {
    const conversation = await getConversation(conversationId);
    if (!conversation || conversation.turn_count !== 0 || (conversation.runs?.length ?? 0) > 0) return;
    await deleteConversation(conversationId);
  } catch {
    // Best effort: an empty conversation left over is harmless.
  }
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

/**
 * The agent and its published versions: from the agent page (loaded once by `AgentLayout`), or
 * loaded here when the tab renders alone (a test).
 */
function useChatAgent(agentId: string): { agent: Agent | undefined; versions: AgentVersion[] | null; agentError: string | null } {
  const page = useAgentPage();
  const shared = page !== null && page.agentId === agentId;
  const [agent, setAgent] = useState<Agent | undefined>(undefined);
  const [versions, setVersions] = useState<AgentVersion[] | null>(null);
  const [agentError, setAgentError] = useState<string | null>(null);

  useEffect(() => {
    if (shared) return;
    let cancelled = false;
    setAgent(undefined);
    setAgentError(null);
    api.agents
      .get(agentId)
      .then((loaded) => {
        if (!cancelled) setAgent(loaded);
      })
      .catch((reason: unknown) => {
        if (!cancelled) setAgentError(reason instanceof ApiError && reason.status === 404 ? "Agent not found." : "Failed to load the agent.");
      });
    return () => {
      cancelled = true;
    };
  }, [agentId, shared]);

  // The agent's published versions (the version selector and each turn's spec).
  useEffect(() => {
    if (shared) return;
    let cancelled = false;
    setVersions(null);
    api.versions
      .list(agentId)
      .then((items) => {
        if (!cancelled) setVersions(Array.isArray(items) ? items : []);
      })
      .catch(() => {
        if (!cancelled) setVersions([]);
      });
    return () => {
      cancelled = true;
    };
  }, [agentId, shared]);

  if (shared) return { agent: page.agent ?? undefined, versions: page.versions, agentError: null };
  return { agent, versions, agentError };
}

export interface ChatTabProps {
  /** The agent, fixed by the route (`/agents/:id/chat`). */
  agentId: string;
  /** The conversation named by the route (`/agents/:id/chat/:conversationId`); none starts a new one. */
  conversationId?: string | null;
}

/**
 * The agent page's Chat tab: the conversation's state and runs (P0.5, P0.17, P0.19) with the agent
 * fixed by the route. It renders the threads panel (`ThreadsPanel`) and the chat column (`ChatPane`)
 * with the composer, whose chips choose the version and model and whose key button opens the request
 * context popover with named presets (SL.10). The conversation is in the path and
 * `?version=&model=` in the query, written back as they change so a reload restores them; the one-shot
 * `context=preset` applies the newest preset and `context=open` opens the popover.
 */
export function ChatTab({ agentId, conversationId: routeConversationId = null }: ChatTabProps) {
  const selectedAgentId = agentId;
  const page = useAgentPage();
  const reportChatHref = page && page.agentId === agentId ? page.setChatHref : undefined;
  const pathname = usePathname();
  const { agent: selectedAgent, versions, agentError } = useChatAgent(agentId);
  const [ready, setReady] = useState(false);
  // An old link to a conversation of another agent: shown as a notice with a link to that chat.
  const [otherAgentConversation, setOtherAgentConversation] = useState<{ agentId: string; conversationId: string } | null>(null);
  const [conversationId, setConversationId] = useState<string | null>(null);
  const [turns, setTurns] = useState<Turn[]>([]);
  const [runActive, setRunActive] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [railRefresh, setRailRefresh] = useState(0);
  const unsubscribeRef = useRef<(() => void) | null>(null);
  const restoredRef = useRef(false);
  // The conversation named by the URL at load time (read once, on the client).
  const initialConversationRef = useRef<string | null>(null);
  // What the address bar shows; each write changes some keys and keeps the others.
  const urlStateRef = useRef<ChatUrlState>({ conversation: null, version: null, model: null });
  // Bumped whenever the thread switches (select, new conversation): a load or a turn
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
  // The preset the context came from (Update and Delete act on it), and a notice about presets.
  const [presetName, setPresetName] = useState<string | null>(null);
  const [presetMessage, setPresetMessage] = useState<string | null>(null);
  const [optionErrors, setOptionErrors] = useState<TurnOptionErrors>(NO_OPTION_ERRORS);
  const [unsentInput, setUnsentInput] = useState<string | null>(null);
  // A refused or failed send puts its text back in the composer (assistant-ui empties it on send).
  const [restoreDraft, setRestoreDraft] = useState<{ text: string; key: number } | null>(null);
  const draftKeyRef = useRef(0);
  const putBack = useCallback((text: string) => setRestoreDraft({ text, key: ++draftKeyRef.current }), []);
  // Applied once: a later remount of the chat pane must not put it back.
  const clearRestoreDraft = useCallback(() => setRestoreDraft(null), []);
  // A conversation created for a first send whose turn was then refused: not shown or put in the URL
  // until a turn starts in it, and reused by the next send (for the same "Act as user") so a refusal
  // leaves no extra conversation.
  // `refused`: its turn start was refused or failed. Only such a conversation is ever discarded: while
  // a start is in flight (the user may switch threads meanwhile) it is not, and once a start succeeds
  // it stops being pending.
  const pendingConversationRef = useRef<{ agentId: string; id: string; actAs: string; resourceId: string | null; refused: boolean } | null>(null);
  // "Act as user" (RM.18): the resource id NEW conversations are created with ("" = the caller).
  const [actAs, setActAs] = useState("");
  // The open conversation's user: a resource id, null (none), undefined (no conversation or loading).
  const [conversationResourceId, setConversationResourceId] = useState<string | null | undefined>(undefined);
  // The URL is written only after it has been read, so a reload never loses its options.
  const [urlReady, setUrlReady] = useState(false);

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
  // The Memory tab and "Act as user" show only for an agent with memory (draft or latest version).
  const memoryChip = agentHasMemory(selectedAgent, versions);

  /** Writes the chat's state to the address bar (replaceState: no history entry, no navigation). */
  const writeUrl = useCallback(
    (patch: Partial<ChatUrlState>) => {
      if (typeof window === "undefined") return;
      urlStateRef.current = { ...urlStateRef.current, ...patch };
      const next = chatUrlFor(window.location.href, agentId, urlStateRef.current);
      if (next !== `${window.location.pathname}${window.location.search}${window.location.hash}`) window.history.replaceState({}, "", next);
      // The Chat tab links back here (conversation and options) from the other tabs.
      reportChatHref?.(next.split("#")[0]);
    },
    [agentId, reportChatHref],
  );
  const setConversationParam = useCallback((id: string | null) => writeUrl({ conversation: id }), [writeUrl]);

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

  /**
   * Select an agent: the context this tab remembers for it, else its newest preset (reading the
   * presets migrates the old saved value into one named "Saved").
   */
  const selectAgent = useCallback((agentId: string, preferPreset = false) => {
    agentRef.current = agentId;
    const preset = agentId ? latestPreset(agentId) : null;
    // `context=preset`: the newest preset replaces the tab's remembered context.
    if (preferPreset && preset !== null) clearPlaygroundContext(agentId);
    const remembered = agentId ? loadPlaygroundContext(agentId) : null;
    const fromPreset = remembered === null && preset !== null;
    setContextValue(remembered ?? preset?.value ?? {});
    setPresetName(fromPreset ? preset.name : null);
    setPresetMessage(fromPreset ? `Using preset “${preset.name}”.` : null);
    setContextValidation(IDLE_VALIDATION);
    setOptionErrors(NO_OPTION_ERRORS);
    setUnsentInput(null);
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
          // Ended on the run's terminal status without an end event: the rail still shows the turn
          // as queued or running (the end events refresh it above).
          if (reason === "terminal_status") refreshRail();
        },
      };
      unsubscribeRef.current =
        initialLastSeq === undefined
          ? subscribeRun(runId, handlers)
          : subscribeRun(runId, handlers, { initialLastSeq });
    },
    [refreshRail, stopSubscription, updateTurn],
  );

  // The options the URL names (`?version=&model=`, and the one-shot `context=preset` or
  // `context=open`), read once on the client; the conversation comes from the route.
  useEffect(() => {
    const initial = parseUrlState(window.location.search);
    initialConversationRef.current = routeConversationId ?? initial.conversation;
    urlStateRef.current = { conversation: initialConversationRef.current, version: initial.version, model: initial.model };
    selectAgent(agentId, initial.preferPreset);
    setActAs(readActAs(agentId));
    if (initial.openContext) setContextOpen(true);
    stripPreferPresetParam();
    setRequestedVersion(initial.version);
    setPendingModelId(initial.model);
    setReady(true);
    setUrlReady(true);
    writeLastAgent(agentId);
    // Read once per mount: a new route (another conversation or agent) mounts a new tab.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

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

  // The target's context fields.
  const contextVersionKey = String(versionRef);
  useEffect(() => {
    let cancelled = false;
    setContextInfo(null);
    setContextLoadError(null);
    // Until the target's fields are known, no earlier validation result applies.
    setContextValidation(IDLE_VALIDATION);
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
    // An agent without a published version always runs its draft; the URL keeps what was asked for
    // until the agent is known, so a reload before then loses nothing.
    writeUrl({ version: selectedAgent ? versionRef : requestedVersion, model: urlModel });
  }, [requestedVersion, selectedAgent, urlModel, urlReady, versionRef, writeUrl]);

  // The Config panel describes the version the chat targets.
  const setConfigVersion = page?.setConfigVersion;
  useEffect(() => {
    setConfigVersion?.(versionRef);
  }, [setConfigVersion, versionRef]);

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
      // Opening another conversation drops a first send's unused conversation, and deletes it while it
      // is still empty (not the one being opened).
      const pending = pendingConversationRef.current;
      pendingConversationRef.current = null;
      if (pending?.refused && pending.id !== id) void discardEmptyConversation(pending.id);
      setConversationId(id);
      setConversationResourceId(undefined);
      setTurns([]);
      setError(null);
      setAnnounceRunId(null);
      setRunActive(true);
      try {
        const conversation = await getConversation(id);
        if (!current()) return;
        if (conversation.agent_id !== agentRef.current) {
          // Another agent's conversation (an old link): it opens in that agent's chat, not here, so
          // none of its events are read.
          setConversationId(null);
          setConversationParam(null);
          setRunActive(false);
          setOtherAgentConversation({ agentId: conversation.agent_id, conversationId: conversation.id });
          return;
        }
        const loaded = await Promise.all(
          conversation.runs.map(async (run) => ({
            run,
            events: await listEvents(run.run_id),
          })),
        );
        if (!current()) return;
        setConversationId(conversation.id);
        setConversationResourceId(conversation.resource_id ?? null);
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
    [followRun, setConversationParam, stopSubscription],
  );

  // Reload: rebuild the thread the route names (`/agents/:id/chat/:conversationId`).
  useEffect(() => {
    if (!ready || restoredRef.current) return;
    restoredRef.current = true;
    const id = initialConversationRef.current;
    if (id) void loadConversation(id);
  }, [ready, loadConversation]);

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
    // A new conversation: the unused one of a refused first send is deleted while still empty.
    const pending = pendingConversationRef.current;
    pendingConversationRef.current = null;
    if (pending?.refused) void discardEmptyConversation(pending.id);
    generationRef.current += 1;
    setConversationId(null);
    setConversationResourceId(undefined);
    setTurns([]);
    setRunActive(false);
    setError(null);
    setAnnounceRunId(null);
    setOtherAgentConversation(null);
    setConversationParam(null);
  }, [setConversationParam, stopSubscription]);

  // A navigation the chat did not write itself (a link to `/agents/:id/chat`, or to another of this
  // agent's conversations) while this tab stays mounted: the screen follows the address. The chat's
  // own replaceState writes are recognised by comparing with what it last wrote.
  const seenPathnameRef = useRef(pathname);
  useEffect(() => {
    if (!ready || !pathname || pathname === seenPathnameRef.current) return;
    seenPathnameRef.current = pathname;
    const target = agentChatPathState(pathname);
    if (!target || target.agentId !== agentId) return;
    if (target.conversation === urlStateRef.current.conversation) return;
    if (target.conversation === null) {
      resetConversation();
    } else {
      setOtherAgentConversation(null);
      urlStateRef.current = { ...urlStateRef.current, conversation: target.conversation };
      void loadConversation(target.conversation);
    }
  }, [agentId, loadConversation, pathname, ready, resetConversation]);

  const handleSelectConversation = useCallback(
    (conversation: Conversation) => {
      if (conversation.id === conversationId) return;
      setOtherAgentConversation(null);
      setConversationParam(conversation.id);
      void loadConversation(conversation.id);
      // On a phone the list is a Sheet over the chat: picking a conversation closes it.
      if (page && !matchesNow(WIDE_QUERY)) page.setThreadsOpen(false);
    },
    [conversationId, loadConversation, page, setConversationParam],
  );

  const handleNewMessage = useCallback(
    async (message: AppendMessage) => {
      const input = messageText(message);
      if (!input || !selectedAgentId || runActive) return;

      const agent = selectedAgent;
      if (!agent) return;
      if (modelChoiceError) {
        setOptionErrors((errors) => ({ ...errors, model: modelChoiceError }));
        setUnsentInput(input);
        putBack(input);
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
      // A new send supersedes any text a refusal was putting back.
      setRestoreDraft(null);
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

      // Read once per send: the chip may change while the request is in flight.
      const newConversationUser = memoryChip ? actAs : "";
      let createdResourceId: string | null = null;
      // The conversation this send created (or reused), for the failure path below.
      let sendConversation: string | null = null;
      try {
        let id = conversationId;
        const firstTurn = id === null;
        if (id === null) {
          // A first send checks its context before creating anything, so a refused context leaves
          // no empty conversation and the address does not move. Only a refusal stops it here: if
          // the check itself fails, the turn start below is the judge.
          if (options.request_context !== null) {
            try {
              await validateRequestContext(selectedAgentId, options.request_context, options.version);
            } catch (refusal) {
              if (refusal instanceof RequestContextValidationError && refusal.status === 422) {
                throw new TurnStartError(422, Object.entries(refusal.fieldErrors).map(([field, msg]) => ({ loc: ["body", "request_context", field], msg, type: "value_error" }))
                  .concat(refusal.banner ? [{ loc: ["body", "request_context"], msg: refusal.banner, type: "value_error" }] : []));
              }
            }
          }
          const pending = pendingConversationRef.current;
          if (pending && pending.agentId === selectedAgentId && pending.actAs === newConversationUser) {
            id = pending.id;
          } else {
            // The empty conversation of a refused first send acts for another user ("Act as user"
            // changed since): its resource id cannot change, so delete it rather than leave it empty.
            if (pending) {
              pendingConversationRef.current = null;
              await discardEmptyConversation(pending.id);
            }
            // No title: the API titles the conversation from its first turn. "Act as user" sets the
            // conversation's resource id, for new conversations only (it never changes after).
            const created = await createConversation(selectedAgentId, null, crypto.randomUUID(), newConversationUser || null);
            id = created.id;
            // A conversation just created has no conversation-scoped note: the inspector needs no
            // read (a 404) until the turn writes one (`memory_updated` forces it).
            primeWorkingMemory(targetKeyOf({ scope: "conversation", conversationId: id })!, null);
            pendingConversationRef.current ={ agentId: selectedAgentId, id, actAs: newConversationUser, resourceId: created.resource_id ?? null, refused: false };
          }
          createdResourceId = pendingConversationRef.current?.id === id ? pendingConversationRef.current.resourceId : null;
          // A reused conversation's start is in flight again: not discardable until it is refused.
          if (pendingConversationRef.current?.id === id) pendingConversationRef.current.refused = false;
          sendConversation = id;
        }
        const started = await startTurnWithOptions(id, runId, input, options);
        if (pendingConversationRef.current?.id === id) pendingConversationRef.current = null;
        refreshRail();
        // The user switched threads meanwhile: the turn is recorded and runs; it shows when its
        // conversation is opened again.
        if (!current()) return;
        // The conversation has its first turn: now it is shown and goes in the address.
        if (firstTurn) {
          setConversationId(id);
          setConversationResourceId(createdResourceId);
          setConversationParam(id);
        }
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
        if (sendConversation !== null) {
          const pending = pendingConversationRef.current;
          if (pending?.id === sendConversation) {
            // Refused or failed: the next send reuses it, a switch discards it.
            pending.refused = true;
          } else if (!current()) {
            // The user left this thread while the start was in flight, and it then failed: nothing
            // else will discard it (the re-read keeps it if the turn was recorded after all).
            void discardEmptyConversation(sendConversation);
          }
        }
        if (!current()) return;
        setTurns((existing) => existing.filter((turn) => turn.runId !== runId));
        setRunActive(false);
        // The text goes back in the composer, ready to send again once the problem is fixed.
        putBack(input);
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
      actAs,
      chosenModelId,
      contextInfo,
      contextValue,
      conversationId,
      defaultModelId,
      followRun,
      memoryChip,
      modelChoiceError,
      putBack,
      refreshRail,
      runActive,
      selectedAgent,
      selectedAgentId,
      setConversationParam,
      spec,
      stopSubscription,
      updateTurn,
      versionRef,
      versions,
    ],
  );

  const handleVersionChange = (ref: VersionRef) => {
    setRequestedVersion(ref);
    // The last check was against the previous version's fields: it says nothing about this one.
    setContextValidation(IDLE_VALIDATION);
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
  const optionsRefused = Boolean(optionErrors.context || optionErrors.model || optionErrors.version || optionErrors.general);

  // Tool cards: each call's status and duration, from its turn's events, keyed by run and call id
  // (a call id is unique only within its run).
  const toolInfos = useMemo(() => {
    const infos = new Map<string, ToolCallInfo>();
    for (const turn of turns) {
      for (const [id, info] of toolCallInfos(turn.events, !TERMINAL_STATUSES.has(turn.displayStatus))) {
        infos.set(toolCallInfoKey(turn.runId, id), info);
      }
    }
    return infos;
  }, [turns]);

  const memorySpec = selectedAgent ? effectiveConfig(selectedAgent, versions, versionRef).spec : null;
  const memoryConfig = memoryOf(memorySpec);
  const latestTurn = useMemo<LatestTurn | null>(
    () => (latest ? { runId: latest.runId, status: latest.displayStatus, events: latest.events } : null),
    [latest],
  );
  // The reflection threshold of the spec the watched turn ran: its published version when the
  // versions are loaded, else (a draft turn, or versions not loaded) the spec the chat runs now.
  // Best-effort: a draft edited since the turn started reads the edited draft's threshold.
  const turnSpec = (latest && !latest.draft && latest.version !== null ? versions?.find((v) => v.version === latest.version)?.spec : null) ?? memorySpec;
  const reflectAfter = memoryOf(turnSpec)?.observational?.reflect_after_tokens;
  const memoryActivity = useMemoryActivity(
    latestTurn,
    {
      observational: Boolean(memoryConfig?.observational),
      semanticRecall: Boolean(memoryConfig?.semantic_recall),
      // The published threshold (the API's default when unset): a reflection can follow an observe.
      reflectAfterTokens: memoryConfig?.observational
        ? typeof reflectAfter === "number"
          ? reflectAfter
          : OBSERVATIONAL_DEFAULTS.reflect_after_tokens
        : null,
    },
    { conversationId: conversationId ?? null },
  );
  const handleConversationDeleted = useCallback(
    (id: string) => {
      if (pendingConversationRef.current?.id === id) pendingConversationRef.current = null;
      if (id === conversationId) resetConversation();
    },
    [conversationId, resetConversation],
  );
  // The inspector's working memory edit, kept here above the panes: a closed threads Sheet, a hidden
  // memory card or the panes' remount at 1024 px does not lose it. While it is unsaved, every way of
  // leaving it asks first through one Discard dialog (`askDiscard`), and leaving the page through the
  // editor's guard (WorkingMemoryGuard).
  const [memoryDraft, setMemoryDraft] = useState<WorkingMemoryDraft | null>(null);
  const [pendingDiscard, setPendingDiscard] = useState<{ proceed: () => void; reason: string } | null>(null);
  // Unsaved only while it is the note on screen: after the version chip moves to a spec with the other
  // working memory scope (or none), the inspector no longer shows it and nothing asks about it. It
  // is kept, so moving back shows it again.
  const inspectorKey = memoryConfig ? targetKeyOf(inspectorTarget(selectedAgentId, conversationId, conversationResourceId, memorySpec).target) : null;
  const memoryDirty = draftDirty(memoryDraft) && memoryDraft?.key === inspectorKey;
  const memoryDirtyRef = useRef(memoryDirty);
  memoryDirtyRef.current = memoryDirty;
  const askDiscard = useCallback((proceed: () => void, reason?: string) => {
    if (memoryDirtyRef.current) setPendingDiscard({ proceed, reason: reason ?? WORKING_MEMORY_GUARD_COPY.description });
    else proceed();
  }, []);
  const askDiscardOnClose = useCallback(
    (close: () => void) => askDiscard(close, "The working memory edit is not saved. Closing the conversations pane discards it."),
    [askDiscard],
  );
  const draftStore = useMemo<WorkingMemoryDraftStore>(() => ({ draft: memoryDraft, setDraft: setMemoryDraft }), [memoryDraft]);
  // Another conversation (after the Discard dialog, or a deleted one): its edit no longer applies.
  useEffect(() => setMemoryDraft(null), [conversationId]);
  const setThreadsCloseGuard = page?.agentId === agentId ? page.setThreadsCloseGuard : undefined;
  useEffect(() => {
    if (!setThreadsCloseGuard) return;
    setThreadsCloseGuard(memoryDirty ? askDiscardOnClose : null);
    return () => setThreadsCloseGuard(null);
  }, [askDiscardOnClose, memoryDirty, setThreadsCloseGuard]);

  const handleActAsChange = (resourceId: string) => {
    setActAs(resourceId);
    writeActAs(selectedAgentId, resourceId);
  };
  const threads = (
    <ThreadsPanel
      agentId={selectedAgent ? selectedAgentId : ""}
      selectedId={conversationId}
      refreshKey={railRefresh}
      onSelect={handleSelectConversation}
      onNew={resetConversation}
      onDeleted={handleConversationDeleted}
      memorySpec={memorySpec}
      memoryInspector={
        selectedAgent && memoryConfig
          ? {
              agentId: selectedAgentId,
              conversationId,
              resourceId: conversationResourceId,
              publishedSpec: latestPublishedSpec(selectedAgent, versions),
              activity: memoryActivity,
              draftStore,
              askDiscard,
            }
          : undefined
      }
    />
  );

  const optionsDisabled = !selectedAgent;
  const modelError = optionErrors.model ?? modelChoiceError;
  const chips = (
    <>
      <ComposerVersionChip
        versions={versions}
        latestVersion={latestVersion}
        versionRef={versionRef}
        onChange={handleVersionChange}
        draftDiffers={draftDiffersFromLatest(selectedAgent?.draft_spec, latestVersion, versions)}
        error={optionErrors.version}
        disabled={optionsDisabled}
      />
      <ComposerModelChip
        defaultModelId={defaultModelId}
        models={models}
        choice={modelChoice}
        onChange={handleModelChoiceChange}
        error={modelError}
        disabled={optionsDisabled}
      />
      {memoryChip ? (
        <ComposerActAsChip
          value={actAs}
          onChange={handleActAsChange}
          conversationUser={conversationId ? (conversationResourceId ?? null) : undefined}
          disabled={optionsDisabled}
        />
      ) : null}
      <RequestContextPopover
        agentId={selectedAgentId}
        versionRef={versionRef}
        info={contextInfo}
        loading={contextLoading}
        loadError={contextLoadError}
        value={contextValue}
        onChange={handleContextChange}
        validation={contextValidation}
        onValidation={setContextValidation}
        turnErrors={optionErrors.context}
        open={contextOpen && !optionsDisabled}
        onOpenChange={setContextOpen}
        presetName={presetName}
        onPresetNameChange={setPresetName}
        presetMessage={presetMessage}
        onPresetMessage={setPresetMessage}
        disabled={optionsDisabled}
      />
    </>
  );
  const notices = (
    <>
      <ComposerFieldError id={VERSION_ERROR_ID} message={optionErrors.version} />
      <ComposerFieldError id={MODEL_ERROR_ID} message={modelError} />
      {!modelError && modelNotice ? <p className="text-caption text-muted-foreground">{modelNotice}</p> : null}
      {!modelError && modelsError ? <p className="text-caption text-muted-foreground">{modelsError}</p> : null}
      {optionErrors.general ? (
        <div role="alert" data-testid="turn-options-error" className="rounded-md border border-destructive/30 bg-destructive/10 px-3 py-2 text-label text-destructive-text">
          {optionErrors.general}
        </div>
      ) : null}
      {optionsRefused && unsentInput ? (
        <p data-testid="unsent-message" className="text-caption text-muted-foreground">
          Not sent: <span className="font-mono">{unsentInput.length > 120 ? `${unsentInput.slice(0, 120)}…` : unsentInput}</span>
        </p>
      ) : null}
    </>
  );
  const hasNotices = Boolean(optionErrors.version || modelError || modelNotice || modelsError || optionErrors.general || (optionsRefused && unsentInput));

  const alert = "rounded-md border px-3 py-2 text-label";
  const header =
    (latest && status) || agentError || otherAgentConversation || status === "recovering" || error || statusError ? (
      <div className="space-y-2">
        {latest && status ? (
          <div className="flex flex-wrap items-center gap-3" data-testid="chat-run-status">
            <StatusPill status={status} showRecoveringNotice={status === "recovering"} />
            <TraceLink runId={latest.runId} tab="events" className="text-label text-info-text hover:underline">
              View run
            </TraceLink>
          </div>
        ) : null}
        {agentError ? (
          <div role="alert" className={`${alert} border-destructive/40 bg-destructive/10 text-destructive-text`}>
            {agentError}
          </div>
        ) : null}
        {otherAgentConversation ? (
          <div role="alert" data-testid="chat-other-agent" className={`${alert} border-warning/40 bg-warning/10 text-warning-text`}>
            This conversation belongs to another agent.{" "}
            <Link
              href={routes.agentChat(otherAgentConversation.agentId, { conversation: otherAgentConversation.conversationId })}
              className="underline underline-offset-2"
            >
              Open it in that agent&apos;s chat
            </Link>
          </div>
        ) : null}
        {status === "recovering" ? (
          <div className={`${alert} border-warning/40 bg-warning/10 text-warning-text`}>
            The worker is restarting. Events may be delayed while the run recovers.
          </div>
        ) : null}
        {error || statusError ? (
          <div role="alert" className={`${alert} border-destructive/40 bg-destructive/10 text-destructive-text`}>
            {error || statusError}
          </div>
        ) : null}
      </div>
    ) : null;

  return (
    // Fills the agent page's body: the conversation scrolls inside, so the card itself never scrolls.
    <div data-testid="playground-page" className="flex h-full min-h-0 min-w-0 flex-1 flex-col">
      <AgentChatPanes agentId={agentId} threads={threads}>
        <div data-testid="agent-chat" className="flex h-full min-h-0 min-w-0 flex-1 flex-col">
          <ChatPane
            messages={thread.messages}
            disabled={composerDisabled}
            running={runActive}
            onNew={handleNewMessage}
            turnViews={turnViews}
            toolInfos={toolInfos}
            landingTitle={selectedAgent?.name ?? "Chat"}
            header={header}
            restoreDraft={restoreDraft}
            onDraftRestored={clearRestoreDraft}
            composer={
              <Composer
                chips={chips}
                outputChecked={hasOutputProcessors(spec)}
                notices={hasNotices ? notices : null}
              />
            }
          />
        </div>
      </AgentChatPanes>
      {memoryDirty ? <WorkingMemoryGuard /> : null}
      {pendingDiscard ? (
        <ConfirmDialog
          title={WORKING_MEMORY_GUARD_COPY.title}
          description={pendingDiscard.reason}
          confirmLabel={WORKING_MEMORY_GUARD_COPY.confirmLabel}
          cancelLabel="Stay"
          destructive
          onConfirm={() => {
            const { proceed } = pendingDiscard;
            setPendingDiscard(null);
            setMemoryDraft(null);
            memoryDirtyRef.current = false;
            proceed();
          }}
          onCancel={() => setPendingDiscard(null)}
        />
      ) : null}
    </div>
  );
}
