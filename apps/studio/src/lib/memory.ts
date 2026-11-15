// Typed client and pure helpers for agent memory (RAG and memory milestone, RM.18): working memory
// notes (conversation or resource scope, edited with If-Match), observations (delete newest or all),
// forget a resource (one agent or every agent), and what a turn's events say about memory (recall
// hits, post-turn updates). Routes: apps/api/src/eap_api/routes/memory.py; deleting a conversation is
// in lib/conversations.ts.
import { BASE_URL, apiHeaders } from "./api";
import { resetWorkingMemoryCache } from "./workingMemoryCache";

// ------------------------------------------------------------------------------------- shapes

export type WorkingMemoryScope = "conversation" | "resource";

/** One working memory note (`WorkingMemoryResponse`). */
export interface WorkingMemoryNote {
  agent_id: string;
  scope: WorkingMemoryScope;
  /** The conversation id (scope `conversation`) or the resource id (scope `resource`). */
  scope_key: string;
  /** `markdown`: `content` is a string (template mode); `json`: an object (schema mode). */
  format: "markdown" | "json";
  content: string | Record<string, unknown>;
  /** Send it back as `If-Match` on PUT and DELETE. */
  version: number;
  updated_by_run_id: string | null;
  created_at: string;
  updated_at: string;
}

/** One observation or reflection (`ObservationResponse`). */
export interface Observation {
  id: string;
  agent_id: string;
  conversation_id: string;
  kind: "observation" | "reflection";
  covers_from_turn: number;
  covers_to_turn: number;
  /** Empty for an expired retention marker. */
  content: string;
  approx_tokens: number;
  run_id: string | null;
  superseded_by: string | null;
  created_at: string;
  /** A retention marker (RM.15): its text aged out, its turns stay covered. Never an empty note. */
  expired?: boolean;
}

export interface ObservationList {
  items: Observation[];
  /** The highest turn the active rows cover (markers included); 0 for none. */
  covered_to_turn: number;
}

/** What a forget removed (`ForgetResourceResponse`). */
export interface ForgetResult {
  resource_id: string;
  /** null: every agent of the tenant. */
  agent_id: string | null;
  working_memories_deleted: number;
  message_embeddings_deleted: number;
  conversations_deleted: string[];
  busy_conversations: string[];
}

/** Which note a working memory call addresses. */
export type WorkingMemoryTarget =
  | { scope: "conversation"; conversationId: string }
  | { scope: "resource"; agentId: string; resourceId: string };

// ------------------------------------------------------------------------- resource id rules

/** eap_run_store.RESOURCE_ID_PATTERN (and `ConversationCreate.resource_id`). */
export const RESOURCE_ID_PATTERN = /^[A-Za-z0-9_.:@-]{1,128}$/;
export const RESOURCE_ID_MAX_LENGTH = 128;

/**
 * Why `value` is not a valid resource id (the server's `validate_resource_id` messages), or null.
 * An empty value is not checked here: callers treat it as "not set".
 */
export function resourceIdProblem(value: string): string | null {
  if (!RESOURCE_ID_PATTERN.test(value)) {
    return "A user id has 1 to 128 characters of A-Z, a-z, 0-9, '_', '.', ':', '@' or '-'.";
  }
  if (!value.replace(/\./g, "")) return "A user id must not consist of dots only.";
  return null;
}

// ------------------------------------------------------------------------------------ requests

/**
 * A refused memory call: the HTTP status, the API's `detail` as the message (a 422 list joined) and,
 * for a busy 409, the conversations. Its own class (not lib/api's ApiError) so modules that mock
 * lib/api in tests can still load this one.
 */
export class MemoryApiError extends Error {
  constructor(
    public status: number,
    message: string,
    public busyConversations: string[] = [],
  ) {
    super(message);
    this.name = "MemoryApiError";
  }
}

/** The HTTP status of a failed call (any error with a numeric `status`), or null. */
export function errorStatus(error: unknown): number | null {
  const status = (error as { status?: unknown } | null)?.status;
  return typeof status === "number" ? status : null;
}

async function failure(response: Response): Promise<MemoryApiError> {
  let message = `HTTP ${response.status}`;
  let busy: string[] = [];
  try {
    const data = await response.json();
    if (typeof data?.detail === "string") message = data.detail;
    else if (Array.isArray(data?.detail)) message = data.detail.map((item: { msg?: unknown }) => String(item?.msg ?? "")).join(", ");
    if (Array.isArray(data?.busy_conversations)) busy = data.busy_conversations.map(String);
  } catch {
    // Not JSON: the status is all there is.
  }
  return new MemoryApiError(response.status, message, busy);
}

async function call(path: string, init: RequestInit = {}): Promise<Response> {
  const response = await fetch(`${BASE_URL}${path}`, {
    ...init,
    headers: apiHeaders({ "Content-Type": "application/json", ...(init.headers as Record<string, string> | undefined) }),
  });
  if (!response.ok) throw await failure(response);
  return response;
}

const enc = encodeURIComponent;

function notePath(target: WorkingMemoryTarget): string {
  return target.scope === "conversation"
    ? `/api/v1/conversations/${enc(target.conversationId)}/memory/working`
    : `/api/v1/agents/${enc(target.agentId)}/memory/resources/${enc(target.resourceId)}/working`;
}

/** The note, or null when there is none (404). */
export async function getWorkingMemory(target: WorkingMemoryTarget): Promise<WorkingMemoryNote | null> {
  try {
    return (await (await call(notePath(target))).json()) as WorkingMemoryNote;
  } catch (error) {
    if (errorStatus(error) === 404) return null;
    throw error;
  }
}

/**
 * Replace the note. `ifMatch` is the version the edit started from (0: the note must not exist
 * yet); a newer note answers 409 ("working memory is at version N, not the If-Match version M…").
 */
export async function putWorkingMemory(
  target: WorkingMemoryTarget,
  content: string | Record<string, unknown>,
  ifMatch: number,
): Promise<WorkingMemoryNote> {
  const response = await call(notePath(target), {
    method: "PUT",
    headers: { "If-Match": `"${ifMatch}"` },
    body: JSON.stringify({ content }),
  });
  return (await response.json()) as WorkingMemoryNote;
}

/**
 * A version conflict (409) in words: the API's detail names the If-Match header ("working memory is
 * at version 4, not the If-Match version 3; reload it…"), which is not for people.
 */
export function conflictMessage(detail: string): string {
  const match = /(?:does not exist \(version 0\)|is at version (\d+)), not the If-Match version (\d+)/.exec(detail);
  if (!match) return "The note changed while you were editing it. Reload it and apply your edit again.";
  const actual = match[1] === undefined ? 0 : Number(match[1]);
  const expected = Number(match[2]);
  if (actual === 0) return "The note was cleared while you were editing it. Reload, then apply your edit again.";
  if (expected === 0) return `A note was written while you were editing (now version ${actual}). Reload it and apply your edit again.`;
  return `The note changed while you were editing it: it is now version ${actual}, and your edit started from version ${expected}. Reload it and apply your edit again.`;
}

/** Clear the note (204 also when there was none); 409 when it changed since `ifMatch`. */
export async function deleteWorkingMemory(target: WorkingMemoryTarget, ifMatch?: number): Promise<void> {
  await call(notePath(target), {
    method: "DELETE",
    headers: ifMatch === undefined ? {} : { "If-Match": `"${ifMatch}"` },
  });
}

/** The agent's notes of one scope, most recently updated first (the Memory tab's user list). */
export async function listAgentWorkingMemories(agentId: string, scope: WorkingMemoryScope, limit = 100): Promise<WorkingMemoryNote[]> {
  const response = await call(`/api/v1/agents/${enc(agentId)}/memory/working?scope=${scope}&limit=${limit}`);
  const data = (await response.json()) as { items?: WorkingMemoryNote[] };
  return Array.isArray(data.items) ? data.items : [];
}

export async function listObservations(conversationId: string, signal?: AbortSignal): Promise<ObservationList> {
  const response = await call(`/api/v1/conversations/${enc(conversationId)}/memory/observations`, signal ? { signal } : {});
  const data = (await response.json()) as Partial<ObservationList>;
  return { items: Array.isArray(data.items) ? data.items : [], covered_to_turn: Number(data.covered_to_turn ?? 0) };
}

/** Delete the newest active observation (an expired marker included); 409 for any other row. */
export async function deleteObservation(conversationId: string, observationId: string): Promise<void> {
  await call(`/api/v1/conversations/${enc(conversationId)}/memory/observations/${enc(observationId)}`, { method: "DELETE" });
}

/** Delete every observation and reflection of the conversation; resolves to how many. */
export async function deleteAllObservations(conversationId: string): Promise<number> {
  const response = await call(`/api/v1/conversations/${enc(conversationId)}/memory/observations`, { method: "DELETE" });
  const data = (await response.json()) as { deleted?: number };
  return Number(data.deleted ?? 0);
}

export interface ForgetOptions {
  /** One agent's memory of the user; omitted: every agent of the tenant. */
  agentId?: string | null;
  /** Also delete the user's conversations and their transcripts. Studio's default is true. */
  includeConversations: boolean;
}

/**
 * Forget a user: `DELETE /agents/{id}/memory/resources/{rid}` (this agent) or
 * `DELETE /memory/resources/{rid}` (all agents), with `include_conversations`. 409 (busy) while a
 * turn of the user is queued or running: nothing is deleted.
 */
export async function forgetResource(resourceId: string, options: ForgetOptions): Promise<ForgetResult> {
  const query = `include_conversations=${options.includeConversations ? "true" : "false"}`;
  const path = options.agentId
    ? `/api/v1/agents/${enc(options.agentId)}/memory/resources/${enc(resourceId)}?${query}`
    : `/api/v1/memory/resources/${enc(resourceId)}?${query}`;
  const response = await call(path, { method: "DELETE" });
  // The user's notes are gone (this agent's, or every agent's, and the deleted conversations'): no
  // cached read may show one again. Every note is dropped, which covers the tenant-wide forget.
  resetWorkingMemoryCache();
  return (await response.json()) as ForgetResult;
}

// ---------------------------------------------------------------------------- observations

/**
 * The row DELETE .../observations/{id} accepts: the active row with the highest `covers_to_turn`,
 * an expired marker included. Null when there is none.
 */
export function newestObservation(items: readonly Observation[]): Observation | null {
  let newest: Observation | null = null;
  for (const item of items) {
    if (item.superseded_by) continue;
    if (!newest || item.covers_to_turn > newest.covers_to_turn) newest = item;
  }
  return newest;
}

/** Whether a newer expired marker blocks deleting `item` (the API's 409 says "expired"). */
export function blockedByExpired(items: readonly Observation[], item: Observation): boolean {
  const newest = newestObservation(items);
  return Boolean(newest && newest.id !== item.id && newest.expired && newest.covers_to_turn > item.covers_to_turn);
}

export function turnRange(item: Pick<Observation, "covers_from_turn" | "covers_to_turn">): string {
  return item.covers_from_turn === item.covers_to_turn ? `Turn ${item.covers_to_turn}` : `Turns ${item.covers_from_turn}–${item.covers_to_turn}`;
}

// ------------------------------------------------------------------------- working memory text

/** The note's content as the editor shows it: Markdown as is, an object as indented JSON. */
export function noteText(content: WorkingMemoryNote["content"] | null | undefined): string {
  if (content === null || content === undefined) return "";
  return typeof content === "string" ? content : JSON.stringify(content, null, 2);
}

/** What a PUT sends for `text`: a string in template mode, a parsed object in schema mode (or an error). */
export function parseNoteText(text: string, format: "markdown" | "json"): { content: string | Record<string, unknown> } | { error: string } {
  if (format === "markdown") return { content: text };
  let value: unknown;
  try {
    value = JSON.parse(text);
  } catch {
    return { error: "Not valid JSON. The agent's working memory uses a JSON schema, so the note is a JSON object." };
  }
  if (value === null || typeof value !== "object" || Array.isArray(value)) {
    return { error: "The note must be a JSON object (the agent's working memory uses a JSON schema)." };
  }
  return { content: value as Record<string, unknown> };
}

// ------------------------------------------------------------------- what a turn says about memory

/** A run event as the API serves it, loosely typed (memory events are not in lib/runs' union). */
export interface LooseRunEvent {
  seq: number;
  type: string;
  data?: Record<string, unknown> | null;
  ts?: string;
}

/** The last turn's semantic recall, from its `embedding` event with `stage: "recall"`. */
export type RecallSummary =
  | { status: "none" }
  | { status: "failed"; errorCode: string | null }
  | { status: "done"; hits: number; turns: number; groups: number; messages: number };

const num = (value: unknown) => (typeof value === "number" && Number.isFinite(value) ? value : 0);

/**
 * What recall brought into the turn. Events carry counts only, never the recalled text. "none": no
 * recall event (nothing was indexed for the user yet, or recall is off).
 */
export function recallSummary(events: readonly LooseRunEvent[]): RecallSummary {
  const event = events.find((item) => item.type === "embedding" && item.data?.stage === "recall");
  if (!event) return { status: "none" };
  const data = event.data ?? {};
  if (data.status === "failed") return { status: "failed", errorCode: typeof data.error_code === "string" ? data.error_code : null };
  return { status: "done", hits: num(data.hits), turns: num(data.turns), groups: num(data.groups), messages: num(data.messages) };
}

/** The events that change what the inspector shows: working memory and observation writes. */
export function memoryWrites(events: readonly LooseRunEvent[]): number {
  return events.filter((item) => item.type === "memory_updated" || item.type === "memory_observe_failed").length;
}

export interface PostTurnFeatures {
  observational: boolean;
  semanticRecall: boolean;
  /**
   * The published `observational.reflect_after_tokens` (default 8000), or null/absent when no
   * reflection is to be waited for. With a conversation to read, the inspector keeps polling past a
   * successful observe while a reflection can follow it (`reflectionDue`).
   */
  reflectAfterTokens?: number | null;
}

/** Whether a turn with these features has post-turn memory steps (the SSE ends before them). */
export function hasPostTurnSteps(features: PostTurnFeatures): boolean {
  return features.observational || features.semanticRecall;
}

const isReflectEvent = (item: LooseRunEvent) =>
  (item.type === "memory_updated" || item.type === "memory_observe_failed") && item.data?.stage === "reflect";

/**
 * Where the observer is in a finished turn's events: `pending` (nothing yet), `observed` (the
 * observation was written: a reflection may follow in the same workflow), or `done` (a reflection
 * reported, or the observe step failed, which ends the post-turn steps).
 */
export function observerProgress(events: readonly LooseRunEvent[]): "pending" | "observed" | "done" {
  if (events.some(isReflectEvent)) return "done";
  if (events.some((item) => item.type === "memory_observe_failed")) return "done";
  if (events.some((item) => item.type === "memory_updated" && item.data?.stage === "observe")) return "observed";
  return "pending";
}

/**
 * Whether the worker reflects after this observe (eap_worker.observational `_reflect_plan`): at
 * least two active rows with text (superseded rows and expired retention markers left out) holding
 * more than `reflectAfterTokens`.
 */
export function reflectionDue(items: readonly Observation[], reflectAfterTokens: number): boolean {
  const active = items.filter((item) => item.superseded_by === null && !item.expired && item.content !== "");
  return active.length >= 2 && active.reduce((sum, item) => sum + num(item.approx_tokens), 0) > reflectAfterTokens;
}

/**
 * Whether the worker wrote `post_turn_done` (P1.C1): the last event of a completed turn's post-turn
 * memory steps, data `{observe: created|skipped|failed|off, index: done|skipped|failed|off}`.
 * Workers from before it never write one; the poll's timeout covers them.
 */
export function postTurnDone(events: readonly LooseRunEvent[]): boolean {
  return events.some((item) => item.type === "post_turn_done");
}

/**
 * Whether the post-turn steps have reported: `post_turn_done` (P1.C1, always the end); else, for
 * workers from before it, the observer's `memory_updated` (stage observe or
 * reflect) or `memory_observe_failed` with observational memory on, else the recall index
 * `embedding` event (stage index). With `awaitReflection` (the observe left enough to reflect over,
 * see `reflectionDue`), a successful observe is not the end: the reflect stage's event is. The
 * observer writes nothing when there was not enough to observe, so the caller also stops after a
 * timeout.
 */
export function postTurnReported(
  events: readonly LooseRunEvent[],
  features: PostTurnFeatures,
  { awaitReflection = false }: { awaitReflection?: boolean } = {},
): boolean {
  if (postTurnDone(events)) return true;
  if (features.observational) {
    const progress = observerProgress(events);
    return progress === "done" || (progress === "observed" && !awaitReflection);
  }
  if (features.semanticRecall) return events.some((item) => item.type === "embedding" && item.data?.stage === "index");
  return true;
}

/** How often and how long the inspector reads a finished turn's events for post-turn updates. */
export const POST_TURN_POLL_MS = 1500;
export const POST_TURN_TIMEOUT_MS = 20_000;
