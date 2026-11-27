"use client";

import { ApiError } from "@/lib/api";
import React, { useCallback, useEffect, useRef, useState } from "react";
import { MoreHorizontal, Plus } from "lucide-react";
import { cn } from "@/lib/cn";
import { ICON_STROKE_WIDTH } from "@/lib/icons";
import { Button, focusRing } from "@/components/ui/Button";
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuTrigger } from "@/components/ui/DropdownMenu";
import { toast } from "@/components/ui/Toaster";
import { ConfirmDialog } from "@/components/shared/ConfirmDialog";
import {
  cloneConversation,
  deleteConversation,
  listConversations,
  renameConversation,
  type Conversation,
} from "@/lib/conversations";

export const CONVERSATION_PAGE_SIZE = 30;
/**
 * A turn's end event comes before its run's terminal status (the worker writes the event first), so
 * the refresh a finished turn triggers can still read "Queued" or "Running". While a listed turn is
 * not finished the first page is read again, `RAIL_SETTLE_MS` apart, at most `RAIL_SETTLE_READS`
 * times after each refresh.
 */
export const RAIL_SETTLE_MS = 1500;
export const RAIL_SETTLE_READS = 5;
const UNFINISHED = new Set(["queued", "running", "recovering", "waiting_approval"]);

/** Whether the row's latest turn has not finished (by its status). */
export function turnUnfinished(conversation: Pick<Conversation, "last_turn_status" | "last_turn_display_status">): boolean {
  const status = conversation.last_turn_display_status ?? conversation.last_turn_status;
  return Boolean(status && UNFINISHED.has(status));
}
const TITLE_MAX_LENGTH = 200;

const STATUS_LABELS: Record<string, string> = {
  queued: "Queued",
  running: "Running",
  recovering: "Recovering",
  waiting_approval: "Waiting for approval",
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
  /** The caller may start, rename, clone and delete conversations of this agent. */
  mayChange?: boolean;
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
  /** Called after a conversation was deleted (or found already gone), with its id (RM.18). */
  onDeleted?: (conversationId: string) => void;
  /** Called after a conversation was cloned, with the new one (already at the top of the list): open it. */
  onCloned?: (conversation: Conversation) => void;
}

/**
 * The playground's left rail: the selected agent's conversations, most recently active first, paged
 * with the API's opaque cursor. Select to load, rename inline (Enter or leaving the field saves,
 * Escape cancels), "New conversation". Arrow Up/Down move between conversations. Each row's menu
 * also clones the conversation (its finished turns, as a new thread) and deletes it (confirmed; RM.18).
 */
export function ConversationRail({
  mayChange = true,
  agentId,
  selectedId,
  refreshKey = 0,
  onSelect,
  onNew,
  onRenamed,
  onDeleted,
  onCloned,
}: ConversationRailProps) {
  const [items, setItems] = useState<Conversation[]>([]);
  const [nextBefore, setNextBefore] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [loadingMore, setLoadingMore] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [draftTitle, setDraftTitle] = useState("");
  const [renameError, setRenameError] = useState<string | null>(null);
  const [deleting, setDeleting] = useState<Conversation | null>(null);
  const [deleteBusy, setDeleteBusy] = useState(false);
  const [deleteError, setDeleteError] = useState<string | null>(null);
  const [cloningId, setCloningId] = useState<string | null>(null);
  const [cloneError, setCloneError] = useState<string | null>(null);
  const newButtonRef = useRef<HTMLButtonElement | null>(null);
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
  // The quiet re-reads while a listed turn is unfinished (see RAIL_SETTLE_MS).
  const [settle, setSettle] = useState(0);
  const settleReadsRef = useRef(0);
  const settleSourceRef = useRef<string | null>(null);
  const settleSeenRef = useRef(0);

  // A rename in progress survives a refresh of the same agent's list, not an agent switch.
  useEffect(() => setEditingId(null), [agentId]);

  // Agent change: start from an empty list (the loading state shows). refreshKey bump: refresh the
  // first page in place, merging it over the pages already loaded with "Load more", so the list,
  // keyboard focus and a rename in progress stay.
  useEffect(() => {
    const request = ++requestRef.current;
    // A new refresh (or agent) starts the re-read budget again; a re-read spends one.
    const source = `${agentId}|${refreshKey}`;
    const settling = settleSourceRef.current === source && settleSeenRef.current !== settle;
    settleSeenRef.current = settle;
    if (settleSourceRef.current !== source) {
      settleSourceRef.current = source;
      settleReadsRef.current = 0;
    }
    let settleTimer: ReturnType<typeof setTimeout> | null = null;
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
    // A re-read is quiet: no loading state, and a failure keeps the list (the next refresh retries).
    if (!settling) {
      setLoading(true);
      setError(null);
    }
    listConversations({ agentId, limit: CONVERSATION_PAGE_SIZE })
      .then((page) => {
        if (request !== requestRef.current) return;
        if (page.items.some(turnUnfinished) && settleReadsRef.current < RAIL_SETTLE_READS) {
          settleReadsRef.current += 1;
          settleTimer = setTimeout(() => setSettle((n) => n + 1), RAIL_SETTLE_MS);
        }
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
        if (request === requestRef.current && !settling) setError("Failed to load conversations.");
      })
      .finally(() => {
        if (request === requestRef.current && !settling) setLoading(false);
      });
    return () => {
      if (settleTimer) clearTimeout(settleTimer);
    };
  }, [agentId, refreshKey, settle]);

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

  // After a rename ends the field unmounts: focus returns to that conversation's row, unless the
  // user already moved it somewhere else (leaving the field also commits the rename).
  const refocusRef = useRef<string | null>(null);
  useEffect(() => {
    const id = refocusRef.current;
    if (id === null || editingId !== null) return;
    refocusRef.current = null;
    const active = document.activeElement;
    if (active && active !== document.body && active.isConnected) return;
    const row = Array.from(listRef.current?.querySelectorAll<HTMLButtonElement>("button[data-conversation]") ?? []).find(
      (button) => button.dataset.conversation === id,
    );
    row?.focus();
  });

  const cancelRename = () => {
    refocusRef.current = editingId;
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
      refocusRef.current = conversation.id;
      setEditingId(null);
      setRenameError(null);
      onRenamed?.(renamed);
    } catch (reason) {
      // A refusal says why; anything else keeps the short message.
      setRenameError(reason instanceof ApiError && reason.status === 403 ? reason.message : "Failed to rename the conversation.");
    } finally {
      savingRef.current = false;
    }
  };

  const confirmDelete = async () => {
    const conversation = deleting;
    if (!conversation) return;
    setDeleteBusy(true);
    setDeleteError(null);
    try {
      await deleteConversation(conversation.id);
      toast.success("Conversation deleted");
    } catch (error) {
      // 404: already gone (deleted elsewhere), which is what was asked. 409: a turn is running.
      if ((error as { status?: unknown } | null)?.status !== 404) {
        setDeleteError(error instanceof Error && error.message ? error.message : "Failed to delete the conversation.");
        setDeleteBusy(false);
        return;
      }
    }
    setItems((current) => current.filter((item) => item.id !== conversation.id));
    setDeleting(null);
    setDeleteBusy(false);
    onDeleted?.(conversation.id);
  };

  const startClone = async (conversation: Conversation) => {
    if (cloningId) return;
    setCloningId(conversation.id);
    setCloneError(null);
    try {
      const cloned = await cloneConversation(conversation.id);
      // The clone is the most recently active: it goes to the top (the list is read again on the
      // next refresh), and the host opens it.
      setItems((current) => [cloned, ...current.filter((item) => item.id !== cloned.id)]);
      toast.success("Conversation cloned");
      onCloned?.(cloned);
    } catch (error) {
      setCloneError(error instanceof Error && error.message ? error.message : "Failed to clone the conversation.");
    } finally {
      setCloningId(null);
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
      {mayChange ? (
        <Button ref={newButtonRef} onClick={onNew} disabled={!agentId} className="mb-2 w-full justify-start">
          <Plus aria-hidden="true" strokeWidth={ICON_STROKE_WIDTH} />
          New conversation
        </Button>
      ) : null}
      <h2 className="mb-1 px-2 text-caption font-semibold uppercase tracking-wide text-muted-foreground">Recent</h2>

      {error && (
        <p role="alert" className="mb-2 px-2 text-label text-destructive-text">
          {error}
        </p>
      )}

      {cloneError && (
        <p role="alert" data-testid="conversation-clone-error" className="mb-2 px-2 text-label text-destructive-text">
          {cloneError}
        </p>
      )}

      {loading && items.length === 0 ? (
        <p role="status" className="px-2 text-label text-muted-foreground">
          Loading conversations…
        </p>
      ) : !agentId ? (
        <p className="px-2 text-label text-muted-foreground">Choose an agent to see its conversations.</p>
      ) : items.length === 0 && !error ? (
        <p className="px-2 text-label text-muted-foreground">
          No conversations yet. Send a message to start one.
        </p>
      ) : (
        <ul ref={listRef} className="min-h-0 flex-1 space-y-0.5 overflow-y-auto">
          {items.map((conversation) => {
            const selected = conversation.id === selectedId;
            const label = conversationLabel(conversation);
            if (editingId === conversation.id) {
              return (
                <li key={conversation.id} className="rounded-md bg-fill-selected p-1.5">
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
                    className={cn("w-full rounded-sm border border-border bg-surface px-2 py-1 text-label text-foreground", focusRing)}
                  />
                  {renameError && (
                    <p role="alert" className="mt-1 text-caption text-destructive-text">
                      {renameError}
                    </p>
                  )}
                </li>
              );
            }
            return (
              <li
                key={conversation.id}
                className={cn("group flex min-w-0 items-start gap-1 rounded-md", selected ? "bg-fill-selected" : "hover:bg-fill-hover")}
              >
                <button
                  type="button"
                  data-conversation={conversation.id}
                  aria-current={selected ? "true" : undefined}
                  onClick={() => onSelect(conversation)}
                  onKeyDown={moveFocus}
                  className={cn("min-w-0 flex-1 rounded-md px-2 py-1.5 text-left", focusRing)}
                >
                  <span className={cn("block truncate text-label text-foreground", selected && "font-semibold")}>{label}</span>
                  <span className="block truncate text-caption text-muted-foreground">{summary(conversation)}</span>
                </button>
                {mayChange ? (
                  <>
                    <button
                      type="button"
                      aria-label={`Rename ${label}`}
                      onClick={() => startRename(conversation)}
                      // Full-strength muted text (>= 4.5:1 on every surface in both themes); no opacity, which drops it under.
                      className={cn("shrink-0 rounded-md px-2 py-1.5 text-caption text-muted-foreground hover:text-foreground", focusRing)}
                    >
                      Rename
                    </button>
                    <DropdownMenu>
                      <DropdownMenuTrigger asChild>
                        <button
                          type="button"
                          aria-label={`Actions for ${label}`}
                          data-testid="conversation-actions"
                          className={cn("inline-flex size-7 shrink-0 items-center justify-center self-center rounded-md text-muted-foreground hover:bg-fill-hover hover:text-foreground", focusRing)}
                        >
                          <MoreHorizontal aria-hidden="true" strokeWidth={ICON_STROKE_WIDTH} className="size-icon" />
                        </button>
                      </DropdownMenuTrigger>
                      <DropdownMenuContent align="end">
                        <DropdownMenuItem onSelect={() => startRename(conversation)}>Rename</DropdownMenuItem>
                        <DropdownMenuItem data-testid="conversation-clone" disabled={cloningId !== null} onSelect={() => void startClone(conversation)}>
                          Clone conversation
                        </DropdownMenuItem>
                        <DropdownMenuItem
                          destructive
                          data-testid="conversation-delete"
                          onSelect={() => {
                            setDeleteError(null);
                            setDeleting(conversation);
                          }}
                        >
                          Delete conversation
                        </DropdownMenuItem>
                      </DropdownMenuContent>
                    </DropdownMenu>
                  </>
                ) : null}
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
          className={cn("mt-2 rounded-md px-2 py-1 text-label text-muted-foreground hover:bg-fill-hover disabled:opacity-50", focusRing)}
        >
          {loadingMore ? "Loading…" : "Load more"}
        </button>
      )}

      {deleting ? (
        <ConfirmDialog
          title="Delete this conversation?"
          description={
            <>
              <p>
                “{conversationLabel(deleting)}” and its memory (observations, its working memory and its recall index) are deleted. Its runs stay
                in Traces for cost and audit, with their text removed.
              </p>
              <p className="mt-2">Memory about the user stays; Forget user on the Memory tab removes it.</p>
            </>
          }
          confirmLabel="Delete conversation"
          destructive
          busy={deleteBusy}
          error={deleteError}
          onConfirm={() => void confirmDelete()}
          onCancel={() => {
            setDeleting(null);
            setDeleteError(null);
          }}
          fallbackFocus={() => newButtonRef.current}
        />
      ) : null}
    </nav>
  );
}
