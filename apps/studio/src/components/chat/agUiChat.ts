// The agent Chat tab over AG-UI (AGUI.12, D21): the agent, the thread history adapter and the small
// pure helpers ChatTab and AgUiChatPane share. Only the chat stream travels over AG-UI; the rail,
// chips, usage, fallback badge and banners read native data by the native `run_id` that
// `CUSTOM pydanticos.run` names (D20).
import type { BaseEvent, RunAgentInput } from "@ag-ui/client";
import { ExportedMessageRepository, type ThreadHistoryAdapter } from "@assistant-ui/react";
import { fromAgUiMessages } from "@assistant-ui/react-ag-ui";
import { tap, type Observable } from "rxjs";
import { BASE_URL, apiHeaders } from "@/lib/api";
import { AgUiHttpError, ResumableHttpAgent, type ResumableHttpAgentConfig } from "@/lib/agui";
import type { RunStatus } from "@/lib/runs";
import type { ModelRefBody, TurnOptions } from "./runOptions";

/** `POST /api/v1/ag-ui/agents/{id}`: the agent's one AG-UI URL (D2). */
export function agUiAgentUrl(agentId: string): string {
  return `${BASE_URL}/api/v1/ag-ui/agents/${encodeURIComponent(agentId)}`;
}

/** `forwardedProps.pydanticos` (`AgUiRunOptions`, camelCase): the turn's run options. */
export interface PydanticosOptions {
  version?: number | "draft" | null;
  model?: ModelRefBody;
  requestContext?: Record<string, unknown>;
  resourceId?: string;
}

/**
 * The run options of one send as `forwardedProps.pydanticos`. `resourceId` ("Act as user") only for
 * a thread without a conversation yet: it is set once, at creation (D3), and another value later is
 * 409 `resource_mismatch`.
 */
export function pydanticosOptions(options: TurnOptions, resourceId: string | null): PydanticosOptions {
  const out: PydanticosOptions = { version: options.version };
  if (options.model) out.model = options.model;
  if (options.request_context !== null) out.requestContext = options.request_context;
  if (resourceId) out.resourceId = resourceId;
  return out;
}

/** What the chat hears from the agent's streams (each call names the run input it belongs to). */
export interface ChatAgentListener {
  onEvent: (event: BaseEvent, input: RunAgentInput) => void;
  /** The stream failed: an HTTP refusal before any event (`AgUiHttpError`), or a network failure. */
  onError: (error: unknown, input: RunAgentInput) => void;
}

/**
 * `ResumableHttpAgent` for the Chat tab:
 * - every POST carries the current run options in `forwardedProps.pydanticos` (`options()`, read
 *   when the run starts; the identical body is re-POSTed on resume);
 * - `attachNext`: the next run the runtime starts attaches to that AG-UI runId with the GET attach
 *   route instead of POSTing (a reload or a thread switch while a turn runs: the history adapter
 *   sets it with `unstable_resume`), so the runtime's own aggregator handles the attached stream,
 *   interrupts included;
 * - `listener` sees every event and failure (`CUSTOM pydanticos.*`, `RUN_FINISHED.result`,
 *   `RUN_ERROR.code`), which the runtime does not expose.
 */
export class StudioChatAgent extends ResumableHttpAgent {
  options: () => PydanticosOptions | null = () => null;
  listener: ChatAgentListener | null = null;
  attachNext: string | null = null;

  constructor(config: ResumableHttpAgentConfig) {
    super(config);
  }

  run(input: RunAgentInput): Observable<BaseEvent> {
    const attach = this.attachNext;
    this.attachNext = null;
    let used = input;
    let source: Observable<BaseEvent>;
    if (attach) {
      used = { ...input, runId: attach };
      this.attachRunId = attach;
      try {
        source = this.connect(used);
      } finally {
        this.attachRunId = undefined;
      }
    } else {
      const options = this.options();
      if (options) used = { ...input, forwardedProps: { ...(input.forwardedProps ?? {}), pydanticos: options } };
      source = super.run(used);
    }
    return source.pipe(
      tap({
        next: (event) => this.listener?.onEvent(event, used),
        error: (error: unknown) => this.listener?.onError(error, used),
      }),
    );
  }

  clone(): StudioChatAgent {
    const cloned = super.clone() as StudioChatAgent;
    cloned.options = this.options;
    cloned.listener = this.listener;
    cloned.attachNext = this.attachNext;
    return cloned;
  }
}

/** The API credential as a plain header map (none configured: no header; the API answers 401). */
function credentialHeaders(): Record<string, string> {
  try {
    const headers: Record<string, string> = {};
    new Headers(apiHeaders()).forEach((value, key) => {
      headers[key] = value;
    });
    return headers;
  } catch {
    return {};
  }
}

/** A new chat agent for one thread (the dev bearer from the API client, like every Studio call). */
export function createChatAgent(agentId: string, threadId: string, config: Partial<ResumableHttpAgentConfig> = {}): StudioChatAgent {
  return new StudioChatAgent({
    url: agUiAgentUrl(agentId),
    headers: credentialHeaders(),
    threadId,
    ...config,
  });
}

/** One turn in the thread GET (`runs`). */
export interface AgUiThreadRun {
  /** The AG-UI runId that started the turn (null for a turn started by the native API). */
  runId: string | null;
  /** The native run id (usage, cost, traces and events are read by it). */
  run_id: string;
  turn: number;
  status: string;
  display_status: string;
  /** A regenerated turn: its messages are left out of `messages` (D5). */
  superseded: boolean;
}

/** `GET /api/v1/ag-ui/agents/{id}/threads/{threadId}`. */
export interface AgUiThread {
  threadId: string;
  conversationId: string | null;
  messages: unknown[];
  runs: AgUiThreadRun[];
  /** The AG-UI runId of an unfinished turn to attach to, else null. */
  activeRunId: string | null;
}

export class AgUiThreadLoadError extends Error {
  constructor(readonly status: number) {
    super(`Failed to load the thread (HTTP ${status})`);
    this.name = "AgUiThreadLoadError";
  }
}

export async function fetchAgUiThread(agentId: string, threadId: string, signal?: AbortSignal): Promise<AgUiThread> {
  const response = await fetch(`${agUiAgentUrl(agentId)}/threads/${encodeURIComponent(threadId)}`, {
    headers: apiHeaders(),
    signal,
  });
  if (!response.ok) throw new AgUiThreadLoadError(response.status);
  const body = (await response.json()) as Partial<AgUiThread>;
  return {
    threadId: body.threadId ?? threadId,
    conversationId: body.conversationId ?? null,
    messages: Array.isArray(body.messages) ? body.messages : [],
    runs: Array.isArray(body.runs) ? body.runs : [],
    activeRunId: typeof body.activeRunId === "string" ? body.activeRunId : null,
  };
}

/** The native run ids of the thread's shown turns, oldest first (superseded turns show no messages). */
export function shownRunIds(thread: Pick<AgUiThread, "runs">): string[] {
  return thread.runs.filter((run) => !run.superseded).map((run) => run.run_id);
}

/**
 * The history adapter (D21): the thread GET, with `unstable_resume` while a turn runs. The resume
 * attaches through `agent.attachNext` (see StudioChatAgent). Messages are the server's (D5): nothing
 * is written back.
 */
export function agUiThreadHistory(
  agentId: string,
  agent: StudioChatAgent,
  onLoaded: (thread: AgUiThread) => void,
  onFailed?: (error: unknown) => void,
): ThreadHistoryAdapter {
  return {
    async load() {
      const threadId = agent.threadId;
      let thread: AgUiThread;
      try {
        thread = await fetchAgUiThread(agentId, threadId);
      } catch (error) {
        onFailed?.(error);
        return { messages: [] };
      }
      onLoaded(thread);
      const repository = ExportedMessageRepository.fromArray(fromAgUiMessages(thread.messages, { showThinking: false }));
      if (thread.activeRunId) agent.attachNext = thread.activeRunId;
      return { ...repository, unstable_resume: thread.activeRunId !== null };
    },
    async append() {
      // Server history is authoritative (D5): the runtime's copy is never stored.
    },
  };
}

/** What ties AG-UI messages to native runs: the D13 id prefix (the AG-UI runId that started a turn,
 * or the native run id for a turn started elsewhere) and the user messages this page sent. */
export interface RunMaps {
  /** Message id prefix (`{prefix}-m{n}`, `{prefix}-t-{id}`, `{prefix}-u`) → native run id. */
  byPrefix: ReadonlyMap<string, string>;
  /** A user message's id (the client's, sent in the run input) → native run id. */
  byUserMessage: ReadonlyMap<string, string>;
}

export const EMPTY_RUN_MAPS: RunMaps = { byPrefix: new Map(), byUserMessage: new Map() };

function prefixRun(id: string, byPrefix: ReadonlyMap<string, string>): string | null {
  for (const [prefix, runId] of byPrefix) {
    if (id.startsWith(`${prefix}-`)) return runId;
  }
  return null;
}

/**
 * Thread message id → the native run id of its turn, from ids only (never by position, so a send
 * that left no run cannot shift later turns): a user message by the id this page sent, else by its
 * own D13 id (`{prefix}-u`), else by the first message after it that carries a known prefix; the
 * other messages by their own prefix, else their user message's run. A message of an unknown turn
 * maps nothing.
 */
export function messageRunMap(messages: readonly { id: string; role: string }[], maps: RunMaps): Map<string, string> {
  const map = new Map<string, string>();
  let current: string | null = null;
  messages.forEach((message, index) => {
    if (message.role === "user") {
      current = maps.byUserMessage.get(message.id) ?? prefixRun(message.id, maps.byPrefix);
      for (let next = index + 1; current === null && next < messages.length && messages[next].role !== "user"; next += 1) {
        current = prefixRun(messages[next].id, maps.byPrefix);
      }
      if (current) map.set(message.id, current);
      return;
    }
    const own = prefixRun(message.id, maps.byPrefix) ?? current;
    if (own) map.set(message.id, own);
  });
  return map;
}

/** The id of the run input's last user message (the client's id the server stores, D13). */
export function inputUserMessageId(input: Pick<RunAgentInput, "messages">): string | null {
  for (let index = input.messages.length - 1; index >= 0; index -= 1) {
    const message = input.messages[index] as { role?: unknown; id?: unknown };
    if (message.role === "user") return typeof message.id === "string" ? message.id : null;
  }
  return null;
}

/** A resume run's answered tool interrupts with what this page sent for each (D15). */
export function resumeDecisions(input: { resume?: unknown }): { runId: string; toolCallId: string; sent: "approved" | "denied" }[] {
  const entries = Array.isArray(input.resume) ? input.resume : [];
  const out: { runId: string; toolCallId: string; sent: "approved" | "denied" }[] = [];
  for (const entry of entries as { interruptId?: unknown; status?: unknown; payload?: unknown }[]) {
    if (typeof entry?.interruptId !== "string") continue;
    const target = toolInterruptTarget({ id: entry.interruptId });
    if (!target) continue;
    const approved = entry.status === "resolved" && (entry.payload as { approved?: unknown } | null)?.approved === true;
    out.push({ runId: target.runId, toolCallId: target.toolCallId, sent: approved ? "approved" : "denied" });
  }
  return out;
}

/** `CUSTOM pydanticos.approval_decided {kind, key, decision}` (D15) for a tool call. */
export function approvalDecidedOf(event: BaseEvent): { toolCallId: string; decision: "approved" | "denied" | "timed_out" } | null {
  const custom = event as BaseEvent & { name?: unknown; value?: unknown };
  if (custom.type !== "CUSTOM" || custom.name !== "pydanticos.approval_decided") return null;
  const value = custom.value as { kind?: unknown; key?: unknown; decision?: unknown } | null;
  if (value?.kind !== "tool" || typeof value.key !== "string") return null;
  const decision = value.decision;
  if (decision !== "approved" && decision !== "denied" && decision !== "timed_out") return null;
  return { toolCallId: value.key, decision };
}

/** `CUSTOM pydanticos.output_retry {model_request, errors}`. */
export function outputRetryEventOf(event: BaseEvent): { model_request: unknown; errors: unknown } | null {
  const custom = event as BaseEvent & { name?: unknown; value?: unknown };
  if (custom.type !== "CUSTOM" || custom.name !== "pydanticos.output_retry") return null;
  const value = custom.value as { model_request?: unknown; errors?: unknown } | null;
  return value ? { model_request: value.model_request, errors: value.errors } : null;
}

/** The text of the run input's last user message (the send a refusal puts back). */
export function inputUserText(input: Pick<RunAgentInput, "messages">): string {
  for (let index = input.messages.length - 1; index >= 0; index -= 1) {
    const message = input.messages[index] as { role?: unknown; content?: unknown };
    if (message.role !== "user") continue;
    if (typeof message.content === "string") return message.content;
    if (Array.isArray(message.content)) {
      return message.content
        .map((part) => (part && typeof part === "object" && (part as { type?: unknown }).type === "text" ? String((part as { text?: unknown }).text ?? "") : ""))
        .join("\n")
        .trim();
    }
    return "";
  }
  return "";
}

const OPTION_AREAS: Record<string, string> = { requestContext: "request_context", model: "model", version: "version", resourceId: "resource_id" };

/**
 * A refusal's `detail` in the native turn-start shape, so `classifyTurnError` puts each message next
 * to its control: `["forwardedProps", "pydanticos", "requestContext", f]` → `["body",
 * "request_context", f]`. Other locations are kept (shown as a general error).
 */
export function nativeDetail(detail: unknown): unknown {
  if (!Array.isArray(detail)) return detail;
  return detail.map((item) => {
    if (!item || typeof item !== "object" || !Array.isArray((item as { loc?: unknown }).loc)) return item;
    const loc = (item as { loc: unknown[] }).loc;
    if (loc[0] === "forwardedProps" && loc[1] === "pydanticos" && typeof loc[2] === "string" && OPTION_AREAS[loc[2]]) {
      return { ...(item as object), loc: ["body", OPTION_AREAS[loc[2]], ...loc.slice(3)] };
    }
    return item;
  });
}

/** An HTTP refusal of the POST (before the stream): its status and native-shaped detail. */
export function refusalOf(error: unknown): { status: number; detail: unknown } | null {
  if (!(error instanceof AgUiHttpError)) return null;
  const payload = error.payload as { detail?: unknown } | string | null;
  const detail = payload && typeof payload === "object" && "detail" in payload ? payload.detail : payload;
  return { status: error.status, detail: nativeDetail(detail) };
}

/** A refusal's text for the chat's error banner. */
export function refusalText(refusal: { status: number; detail: unknown }): string {
  const { detail, status } = refusal;
  if (typeof detail === "string" && detail) return detail;
  if (Array.isArray(detail)) {
    const text = detail.map((item) => String((item as { msg?: unknown })?.msg ?? "")).filter(Boolean).join(", ");
    if (text) return text;
  }
  if (detail && typeof detail === "object" && typeof (detail as { message?: unknown }).message === "string") {
    return (detail as { message: string }).message;
  }
  return `HTTP ${status}`;
}

/** `CUSTOM pydanticos.run` (D7): the native ids of the turn being streamed. */
export interface PydanticosRunInfo {
  run_id: string;
  conversation_id: string | null;
  turn: number | null;
  trace_id: string | null;
  model_id: string | null;
  version: number | null;
  draft: boolean;
}

export function runInfoOf(event: BaseEvent): PydanticosRunInfo | null {
  const custom = event as BaseEvent & { name?: unknown; value?: unknown };
  if (custom.type !== "CUSTOM" || custom.name !== "pydanticos.run") return null;
  const value = custom.value as Record<string, unknown> | null;
  if (!value || typeof value.run_id !== "string") return null;
  return {
    run_id: value.run_id,
    conversation_id: typeof value.conversation_id === "string" ? value.conversation_id : null,
    turn: typeof value.turn === "number" ? value.turn : null,
    trace_id: typeof value.trace_id === "string" ? value.trace_id : null,
    model_id: typeof value.model_id === "string" ? value.model_id : null,
    version: typeof value.version === "number" ? value.version : null,
    draft: value.draft === true,
  };
}

const DISPLAY_STATUSES = new Set<RunStatus>(["queued", "running", "recovering", "waiting_approval", "completed", "failed", "needs_attention"] as RunStatus[]);

/** `CUSTOM pydanticos.status {display_status}` (D8): recovering, waiting_approval, running. */
export function displayStatusOf(event: BaseEvent): RunStatus | null {
  const custom = event as BaseEvent & { name?: unknown; value?: unknown };
  if (custom.type !== "CUSTOM" || custom.name !== "pydanticos.status") return null;
  const status = (custom.value as { display_status?: unknown } | null)?.display_status;
  return typeof status === "string" && DISPLAY_STATUSES.has(status as RunStatus) ? (status as RunStatus) : null;
}

/** The interrupt id of a tool approval (D15): `approval:{run_id}:tool:{tool_call_id}`. */
export function toolInterruptTarget(interrupt: { id: string; toolCallId?: string; metadata?: Record<string, unknown> }): { runId: string; toolCallId: string; toolName: string | null } | null {
  const meta = (interrupt.metadata?.pydanticos ?? null) as { run_id?: unknown; tool_name?: unknown } | null;
  const match = /^approval:(.+):tool:(.+)$/.exec(interrupt.id);
  const runId = typeof meta?.run_id === "string" ? meta.run_id : match?.[1];
  const toolCallId = interrupt.toolCallId ?? match?.[2];
  if (!runId || !toolCallId) return null;
  return { runId, toolCallId, toolName: typeof meta?.tool_name === "string" ? meta.tool_name : null };
}
