"use client";

import React, { createContext, useCallback, useContext, useState } from "react";
import { ThumbsDown, ThumbsUp } from "lucide-react";
import { cn } from "@/lib/cn";
import { ICON_STROKE_WIDTH } from "@/lib/icons";
import { focusRing } from "@/components/ui/Button";
import {
  clearMessageFeedback,
  setMessageFeedback,
  type FeedbackMap,
  type FeedbackRating,
  type MessageFeedbackEntry,
} from "@/lib/messageFeedback";

export const FEEDBACK_COMMENT_MAX = 2000;

/** The thread's thumbs: the conversation they are recorded on and the caller's own ratings. */
export interface FeedbackScope {
  conversationId: string | null;
  initial: FeedbackMap;
}

const NO_SCOPE: FeedbackScope = { conversationId: null, initial: {} };
export const FeedbackScopeContext = createContext<FeedbackScope>(NO_SCOPE);

/** Only a model answer (`{runId}-m{n}`) can be rated. */
export function isRatableMessage(messageId: string): boolean {
  return /-m\d+$/.test(messageId);
}

/**
 * Thumbs up / down under an assistant message, with an optional comment. A thumb toggles: pressing
 * the active one clears the rating; the other replaces it. The state comes from the thread GET, so it
 * shows again after a reload.
 */
export function MessageFeedback({ messageId }: { messageId: string }) {
  const scope = useContext(FeedbackScopeContext);
  const [overrides, setOverrides] = useState<Record<string, MessageFeedbackEntry | null>>({});
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [commenting, setCommenting] = useState(false);
  const [draft, setDraft] = useState("");
  const conversationId = scope.conversationId;
  const current: MessageFeedbackEntry | null = messageId in overrides ? overrides[messageId] : (scope.initial[messageId] ?? null);

  const save = useCallback(
    async (rating: FeedbackRating, comment: string | null) => {
      if (!conversationId) return;
      setBusy(true);
      setError(null);
      try {
        const saved = await setMessageFeedback(conversationId, messageId, rating, comment);
        setOverrides((existing) => ({ ...existing, [messageId]: saved }));
        return true;
      } catch {
        setError("Could not save your feedback.");
        return false;
      } finally {
        setBusy(false);
      }
    },
    [conversationId, messageId],
  );

  const press = async (rating: FeedbackRating) => {
    if (!conversationId || busy) return;
    if (current?.rating === rating) {
      setBusy(true);
      setError(null);
      try {
        await clearMessageFeedback(conversationId, messageId);
        setOverrides((existing) => ({ ...existing, [messageId]: null }));
        setCommenting(false);
      } catch {
        setError("Could not clear your feedback.");
      } finally {
        setBusy(false);
      }
      return;
    }
    if (await save(rating, null)) {
      setDraft("");
      setCommenting(rating === "down");
    }
  };

  const submitComment = async () => {
    if (!current) return;
    if (await save(current.rating, draft.trim() || null)) setCommenting(false);
  };

  const thumbClass = (rating: FeedbackRating) =>
    cn(
      "inline-flex size-7 items-center justify-center rounded-md text-muted-foreground hover:bg-fill-hover hover:text-foreground disabled:pointer-events-none disabled:opacity-50",
      current?.rating === rating && "bg-fill-selected text-foreground",
      focusRing,
    );

  if (!conversationId) return null;
  return (
    <div data-testid="message-feedback" className="mt-1 flex min-w-0 flex-col gap-1.5">
      <div role="group" aria-label="Rate this answer" className="flex items-center gap-0.5">
        <button
          type="button"
          aria-label="Good answer"
          aria-pressed={current?.rating === "up"}
          disabled={busy}
          data-testid="feedback-up"
          onClick={() => void press("up")}
          className={thumbClass("up")}
        >
          <ThumbsUp aria-hidden="true" strokeWidth={ICON_STROKE_WIDTH} className="size-4" />
        </button>
        <button
          type="button"
          aria-label="Bad answer"
          aria-pressed={current?.rating === "down"}
          disabled={busy}
          data-testid="feedback-down"
          onClick={() => void press("down")}
          className={thumbClass("down")}
        >
          <ThumbsDown aria-hidden="true" strokeWidth={ICON_STROKE_WIDTH} className="size-4" />
        </button>
        {current?.comment && !commenting ? (
          <span data-testid="feedback-comment" className="ml-1.5 min-w-0 truncate text-caption text-muted-foreground">{current.comment}</span>
        ) : null}
        {current && !commenting ? (
          <button
            type="button"
            data-testid="feedback-add-comment"
            onClick={() => { setDraft(current.comment ?? ""); setCommenting(true); }}
            className={cn("ml-1 rounded px-1.5 text-caption text-muted-foreground hover:text-foreground", focusRing)}
          >
            {current.comment ? "Edit comment" : "Add comment"}
          </button>
        ) : null}
      </div>
      {commenting && current ? (
        <form
          className="flex min-w-0 flex-col gap-1.5"
          onSubmit={(event) => { event.preventDefault(); void submitComment(); }}
        >
          <textarea
            aria-label="Feedback comment"
            value={draft}
            maxLength={FEEDBACK_COMMENT_MAX}
            rows={2}
            onChange={(event) => setDraft(event.target.value)}
            className="min-w-0 resize-none rounded-md border border-border bg-surface px-2 py-1 text-body text-foreground focus-visible:outline-none focus-visible:border-ring"
          />
          <div className="flex gap-1.5">
            <button type="submit" disabled={busy} className={cn("rounded-md bg-primary px-2.5 py-1 text-caption text-primary-foreground hover:bg-primary/90 disabled:opacity-50", focusRing)}>Save comment</button>
            <button type="button" onClick={() => setCommenting(false)} className={cn("rounded-md px-2.5 py-1 text-caption text-muted-foreground hover:bg-fill-hover", focusRing)}>Cancel</button>
          </div>
        </form>
      ) : null}
      {error ? <p role="alert" className="text-caption text-destructive-text">{error}</p> : null}
    </div>
  );
}
