"use client";

import React, { useCallback, useEffect, useId, useState } from "react";
import { ThumbsDown, ThumbsUp } from "lucide-react";
import { Button } from "@/components/ui/Button";
import { toast } from "@/components/ui/Toaster";
import { ICON_STROKE_WIDTH } from "@/lib/icons";
import { addTraceFeedback, listTraceFeedback, type FeedbackRating, type TraceFeedback as Feedback } from "@/lib/traces";

const field =
  "rounded-md border border-input bg-surface px-2 text-label text-foreground placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring";

/**
 * Feedback on a trace (no `spanId`) or on one of its spans: a thumbs up or down, an optional
 * comment and numeric score, and the feedback already left (newest first, with who and when).
 */
export function TraceFeedback({ runId, spanId = null, className }: { runId: string; spanId?: string | null; className?: string }) {
  const id = useId();
  const [items, setItems] = useState<Feedback[] | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [rating, setRating] = useState<FeedbackRating | null>(null);
  const [comment, setComment] = useState("");
  const [score, setScore] = useState("");
  const [saving, setSaving] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);

  const load = useCallback(async (signal?: AbortSignal) => {
    try {
      const all = await listTraceFeedback(runId, { spanId, signal });
      // The trace's own feedback excludes its spans' (they show on the span).
      setItems(spanId ? all : all.filter((f) => f.span_id === null));
      setLoadError(null);
    } catch (err) {
      if (signal?.aborted) return;
      setItems([]);
      setLoadError(err instanceof Error && err.message ? err.message : "Could not load feedback");
    }
  }, [runId, spanId]);

  useEffect(() => {
    setItems(null);
    setRating(null);
    setComment("");
    setScore("");
    setFormError(null);
    const controller = new AbortController();
    void load(controller.signal);
    return () => controller.abort();
  }, [load]);

  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    if (!rating) {
      setFormError("Choose thumbs up or thumbs down.");
      return;
    }
    const scoreText = score.trim();
    const scoreValue = scoreText === "" ? null : Number(scoreText);
    if (scoreValue !== null && !Number.isFinite(scoreValue)) {
      setFormError("The score must be a number.");
      return;
    }
    setSaving(true);
    setFormError(null);
    try {
      await addTraceFeedback(runId, { rating, comment: comment.trim() || null, score: scoreValue, span_id: spanId });
      setRating(null);
      setComment("");
      setScore("");
      toast("Feedback saved");
      await load();
    } catch (err) {
      setFormError(err instanceof Error && err.message ? err.message : "Could not save the feedback");
    } finally {
      setSaving(false);
    }
  };

  const noun = spanId ? "span" : "trace";
  return (
    <section aria-labelledby={`${id}-title`} className={`flex min-w-0 flex-col gap-3 ${className ?? ""}`} data-testid={spanId ? "span-feedback" : "trace-feedback"}>
      <h3 id={`${id}-title`} className="text-label font-semibold text-foreground">Feedback</h3>
      <form onSubmit={submit} className="flex min-w-0 flex-col gap-2" aria-label={`Leave feedback on this ${noun}`}>
        <div role="group" aria-label="Rating" className="flex gap-2">
          <Button type="button" size="sm" variant={rating === "up" ? "primary" : "secondary"} aria-pressed={rating === "up"} onClick={() => setRating(rating === "up" ? null : "up")}>
            <ThumbsUp className="size-icon" strokeWidth={ICON_STROKE_WIDTH} aria-hidden="true" />
            Thumbs up
          </Button>
          <Button type="button" size="sm" variant={rating === "down" ? "primary" : "secondary"} aria-pressed={rating === "down"} onClick={() => setRating(rating === "down" ? null : "down")}>
            <ThumbsDown className="size-icon" strokeWidth={ICON_STROKE_WIDTH} aria-hidden="true" />
            Thumbs down
          </Button>
        </div>
        <label htmlFor={`${id}-comment`} className="text-caption font-medium text-muted-foreground">Comment (optional)</label>
        <textarea id={`${id}-comment`} value={comment} maxLength={2000} rows={2} onChange={(e) => setComment(e.target.value)} className={`${field} w-full py-1`} />
        <label htmlFor={`${id}-score`} className="text-caption font-medium text-muted-foreground">Score (optional)</label>
        <input id={`${id}-score`} type="number" step="any" inputMode="decimal" value={score} onChange={(e) => setScore(e.target.value)} className={`${field} h-8 w-32`} />
        {formError ? <p role="alert" className="text-label text-destructive-text">{formError}</p> : null}
        <Button type="submit" size="sm" variant="primary" disabled={saving} className="self-start">
          {saving ? "Saving..." : "Send feedback"}
        </Button>
      </form>
      {loadError ? <p role="alert" className="text-label text-destructive-text">{loadError}</p> : null}
      {items && items.length > 0 ? (
        <ul className="flex flex-col gap-2" aria-label={`Feedback on this ${noun}`} data-testid="feedback-list">
          {items.map((f) => (
            <li key={f.id} className="rounded-md border border-border bg-card p-2 text-label" data-testid="feedback-item">
              <p className="flex flex-wrap items-center gap-2">
                <span className="font-medium">{f.rating === "up" ? "Thumbs up" : "Thumbs down"}</span>
                {f.score !== null ? <span className="font-mono text-caption">score {f.score}</span> : null}
                <span className="text-caption text-muted-foreground">{f.created_by} · {new Date(f.created_at).toLocaleString()}</span>
              </p>
              {f.comment ? <p className="mt-1 whitespace-pre-wrap break-words">{f.comment}</p> : null}
            </li>
          ))}
        </ul>
      ) : items ? (
        <p className="text-label text-muted-foreground">No feedback yet.</p>
      ) : null}
    </section>
  );
}
