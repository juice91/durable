// Typed client for conversations: a thread with one agent whose turns are runs.
import { BASE_URL, apiErrorFromResponse, apiHeaders } from "./api";
import type { Run } from "./runs";

export interface Conversation {
  id: string;
  agent_id: string;
  title: string | null;
  turn_count: number;
  /** The latest turn's run, or null for a conversation without turns. */
  last_turn_run_id?: string | null;
  last_turn_status?: string | null;
  last_turn_display_status?: Run["display_status"] | null;
  created_at: string;
  updated_at: string;
}

/** One page of GET /conversations, most recently active first. */
export interface ConversationPage {
  items: Conversation[];
  /** Opaque cursor for the next page, or null when this was the last page. */
  next_before: string | null;
}

/** A run that is a conversation turn (turn numbers start at 1). */
export interface ConversationRun extends Run {
  conversation_id: string | null;
  turn: number | null;
}

export interface ConversationDetail extends Conversation {
  /** The conversation's runs, in turn order. */
  runs: ConversationRun[];
}

export interface StartedTurn {
  run_id: string;
  status: string;
  display_status: Run["display_status"];
  agent_id: string;
  version: number;
  conversation_id: string;
  turn: number;
}

async function send<T>(path: string, init: RequestInit = {}): Promise<T> {
  const response = await fetch(`${BASE_URL}${path}`, {
    ...init,
    headers: { "Content-Type": "application/json", ...apiHeaders(init.headers ?? {}) },
  });
  if (!response.ok) throw await apiErrorFromResponse(response);
  return response.json();
}

/** Create a conversation; `conversationId` (client-generated) makes a retry idempotent. */
export function createConversation(
  agentId: string,
  title: string | null,
  conversationId?: string,
): Promise<Conversation> {
  return send(`/api/v1/agents/${encodeURIComponent(agentId)}/conversations`, {
    method: "POST",
    body: JSON.stringify({ title, ...(conversationId ? { conversation_id: conversationId } : {}) }),
  });
}

export function getConversation(conversationId: string): Promise<ConversationDetail> {
  return send(`/api/v1/conversations/${encodeURIComponent(conversationId)}`);
}

export function listConversations(
  options: { agentId?: string; limit?: number; before?: string | null } = {},
): Promise<ConversationPage> {
  const params = new URLSearchParams();
  if (options.agentId) params.set("agent_id", options.agentId);
  if (options.limit) params.set("limit", String(options.limit));
  if (options.before) params.set("before", options.before);
  const query = params.toString();
  return send(`/api/v1/conversations${query ? `?${query}` : ""}`);
}

/** Rename (1–200 characters, one line); the list order does not change. */
export function renameConversation(conversationId: string, title: string): Promise<Conversation> {
  return send(`/api/v1/conversations/${encodeURIComponent(conversationId)}`, {
    method: "PATCH",
    body: JSON.stringify({ title }),
  });
}

/**
 * Start the next turn. Idempotent on `runId`: retrying the same turn returns it (200) and re-enqueues
 * it, so a turn whose start was interrupted heals. The API answers 409 while the previous turn runs.
 */
export function startTurn(conversationId: string, runId: string, input: string): Promise<StartedTurn> {
  return send(`/api/v1/conversations/${encodeURIComponent(conversationId)}/runs`, {
    method: "POST",
    body: JSON.stringify({ run_id: runId, input }),
  });
}
