"use client";

import React, { useCallback, useEffect, useRef, useState } from "react";
import {
  listConversations,
  renameConversation,
  type Conversation,
} from "@/lib/conversations";

export const CONVERSATION_PAGE_SIZE = 30;
const TITLE_MAX_LENGTH = 200;

const STATUS_LABELS: Record<string, string> = {
  queued: "Queued",
  running: "Running",
  recovering: "Recovering",
  completed: "Completed",
  failed: "Failed",
  needs_attention: "Needs attention",
};

export function conversationLabel(conversation: Pick<Conversation, "title">): string {
  return conversation.title?.trim() || "Untitled conversation";
}

function summary(conversation: Conversation): string {
  const turns = `${conversation.turn_count} ${conversation.turn_count === 1 ? "turn" : "turns"}`;
  const status = conversation.last_turn_display_status ?? conversation.last_turn_status;
  return status ? `${turns} · ${STATUS_LABELS[status] ?? status}` : turns;
}

export interface ConversationRailProps {
  /** The selected agent; the rail lists its conversations only. Empty: nothing to list. */
  agentId: string;
  /** The conversation shown in the thread, highlighted and marked aria-current. */
  selectedId: string | null;
  /** Bump to reload the first page (a conversation was created, a turn started or ended). */
  refreshKey?: number;
  onSelect: (conversation: Conversation) => void;
  onNew: () => void;
  /** Called after a successful rename, with the updated conversation. */
  onRenamed?: (conversation: Conversation) => void;
}

/**
 * The playground's left rail: the selected agent's conversations, most recently active first, paged
 * with the API's opaque cursor. Select to load, rename inline (Enter or leaving the field saves,
 * Escape cancels), "New conversation". Arrow Up/Down move between conversations.
 */
export function ConversationRail({
  agentId,
  selectedId,
  refreshKey = 0,
  onSelect,
  onNew,
  onRenamed,
}: ConversationRailProps) {
  const [items, setItems] = useState<Conversation[]>([]);
  const [nextBefore, setNextBefore] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [loadingMore, setLoadingMore] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [draftTitle, setDraftTitle] = useState("");
  const [renameError, setRenameError] = useState<string | null>(null);
  // Only the latest request may write: an agent switch or refresh makes older responses stale.
  const requestRef = useRef(0);
  const listRef = useRef<HTMLUListElement | null>(null);
  const savingRef = useRef(false);
  // What the last refresh merges into, and which agent the shown items belong to.
  const itemsRef = useRef<Conversation[]>([]);
  const nextBeforeRef = useRef<string | null>(null);
  const shownAgentRef = useRef<string | null>(null);
  itemsRef.current = items;
  nextBeforeRef.current = nextBefore;

  // A rename in progress survives a refresh of the same agent's list, not an agent switch.
  useEffect(() => setEditingId(null), [agentId]);

  // Agent change: start from an empty list (the loading state shows). refreshKey bump: refresh the
  // first page in place, merging it over the pages already loaded with "Load more", so the list,
  // keyboard focus and a rename in progress stay.
  useEffect(() => {
    const request = ++requestRef.current;
    const agentChanged = shownAgentRef.current !== agentId;
    shownAgentRef.current = agentId;
    if (agentChanged) {
      setItems([]);
      setNextBefore(null);
    }
    if (!agentId) {
      setLoading(false);
      setError(null);
      return;
    }
    setLoading(true);
    setError(null);
    listConversations({ agentId, limit: CONVERSATION_PAGE_SIZE })
      .then((page) => {
        if (request !== requestRef.current) return;
        const previous = agentChanged ? [] : itemsRef.current;
        if (previous.length <= CONVERSATION_PAGE_SIZE) {
          setItems(page.items);
          setNextBefore(page.next_before);
          return;
        }
        // More than one page was loaded: the new first page, then the rest in their loaded order;
        // the cursor still names the last loaded conversation.
        const fresh = new Set(page.items.map((item) => item.id));
        setItems([...page.items, ...previous.filter((item) => !fresh.has(item.id))]);
        setNextBefore(nextBeforeRef.current);
      })
      .catch(() => {
        if (request === requestRef.current) setError("Failed to load conversations.");
      })
      .finally(() => {
        if (request === requestRef.current) setLoading(false);
      });
  }, [agentId, refreshKey]);

  const loadMore = useCallback(() => {
    if (!nextBefore || loadingMore) return;
    const request = requestRef.current;
    setLoadingMore(true);
    listConversations({ agentId, limit: CONVERSATION_PAGE_SIZE, before: nextBefore })
      .then((page) => {
        if (request !== requestRef.current) return;
        setItems((current) => {
          const seen = new Set(current.map((item) => item.id));
          return [...current, ...page.items.filter((item) => !seen.has(item.id))];
        });
        setNextBefore(page.next_before);
      })
      .catch(() => {
        if (request === requestRef.current) setError("Failed to load more conversations.");
      })
      .finally(() => setLoadingMore(false));
  }, [agentId, loadingMore, nextBefore]);

  const startRename = (conversation: Conversation) => {
    setEditingId(conversation.id);
    setDraftTitle(conversation.title ?? "");
    setRenameError(null);
  };

  const cancelRename = () => {
    setEditingId(null);
    setRenameError(null);
  };

  const saveRename = async (conversation: Conversation) => {
    if (savingRef.current) return;
    const title = draftTitle.trim();
    if (title === (conversation.title ?? "").trim()) {
      cancelRename();
      return;
    }
    if (title.length < 1 || title.length > TITLE_MAX_LENGTH) {
      setRenameError(`A title has 1 to ${TITLE_MAX_LENGTH} characters.`);
      return;
    }
    savingRef.current = true;
    try {
      const renamed = await renameConversation(conversation.id, title);
      setItems((current) => current.map((item) => (item.id === renamed.id ? { ...item, ...renamed } : item)));
      setEditingId(null);
      setRenameError(null);
      onRenamed?.(renamed);
    } catch {
      setRenameError("Failed to rename the conversation.");
    } finally {
      savingRef.current = false;
    }
  };

  const moveFocus = (event: React.KeyboardEvent<HTMLButtonElement>) => {
    if (event.key !== "ArrowDown" && event.key !== "ArrowUp") return;
    const buttons = Array.from(
      listRef.current?.querySelectorAll<HTMLButtonElement>("button[data-conversation]") ?? [],
    );
    const index = buttons.indexOf(event.currentTarget);
    const next = buttons[index + (event.key === "ArrowDown" ? 1 : -1)];
    if (next) {
      event.preventDefault();
      next.focus();
    }
  };

  return (
    <nav
      aria-label="Conversations"
      className="flex min-h-0 w-full min-w-0 flex-1 flex-col"
    >
      <button
        type="button"
        onClick={onNew}
        disabled={!agentId}
        className="mb-3 rounded-md border border-border px-3 py-2 text-sm font-medium hover:bg-muted disabled:cursor-not-allowed disabled:opacity-50"
      >
        New conversation
      </button>

      {error && (
        <p role="alert" className="mb-2 text-sm text-destructive-text">
          {error}
        </p>
      )}

      {loading && items.length === 0 ? (
        <p role="status" className="text-sm text-muted-foreground">
          Loading conversations…
        </p>
      ) : !agentId ? (
        <p className="text-sm text-muted-foreground">Choose an agent to see its conversations.</p>
      ) : items.length === 0 && !error ? (
        <p className="text-sm text-muted-foreground">
          No conversations yet. Send a message to start one.
        </p>
      ) : (
        <ul ref={listRef} className="min-h-0 flex-1 space-y-1 overflow-y-auto">
          {items.map((conversation) => {
            const selected = conversation.id === selectedId;
            const label = conversationLabel(conversation);
            if (editingId === conversation.id) {
              return (
                <li key={conversation.id} className="rounded-md bg-muted p-2">
                  <input
                    aria-label="Conversation title"
                    autoFocus
                    maxLength={TITLE_MAX_LENGTH}
                    value={draftTitle}
                    onChange={(event) => setDraftTitle(event.target.value)}
                    onKeyDown={(event) => {
                      if (event.key === "Enter") {
                        event.preventDefault();
                        void saveRename(conversation);
                      } else if (event.key === "Escape") {
                        event.preventDefault();
                        cancelRename();
                      }
                    }}
                    onBlur={() => void saveRename(conversation)}
                    className="w-full rounded border border-border bg-card px-2 py-1 text-sm text-card-foreground"
                  />
                  {renameError && (
                    <p role="alert" className="mt-1 text-xs text-destructive-text">
                      {renameError}
                    </p>
                  )}
                </li>
              );
            }
            return (
              <li
                key={conversation.id}
                className={`group flex items-start gap-1 rounded-md ${selected ? "bg-muted" : "hover:bg-muted"}`}
              >
                <button
                  type="button"
                  data-conversation={conversation.id}
                  aria-current={selected ? "true" : undefined}
                  onClick={() => onSelect(conversation)}
                  onKeyDown={moveFocus}
                  className="min-w-0 flex-1 px-2 py-2 text-left"
                >
                  <span className={`block truncate text-sm ${selected ? "font-semibold" : ""}`}>{label}</span>
                  <span className="block text-xs text-muted-foreground">{summary(conversation)}</span>
                </button>
                <button
                  type="button"
                  aria-label={`Rename ${label}`}
                  onClick={() => startRename(conversation)}
                  className="rounded px-2 py-2 text-xs text-muted-foreground opacity-70 hover:text-foreground focus:opacity-100 group-hover:opacity-100"
                >
                  Rename
                </button>
              </li>
            );
          })}
        </ul>
      )}

      {!(loading && items.length === 0) && nextBefore && (
        <button
          type="button"
          onClick={loadMore}
          disabled={loadingMore}
          className="mt-2 rounded-md px-3 py-1 text-sm text-muted-foreground hover:bg-muted disabled:opacity-50"
        >
          {loadingMore ? "Loading…" : "Load more"}
        </button>
      )}
    </nav>
  );
}
