"use client";

import React, { useEffect, useId, useState } from "react";
import { ChevronDown, UserRound } from "lucide-react";
import { cn } from "@/lib/cn";
import { ICON_STROKE_WIDTH } from "@/lib/icons";
import { RESOURCE_ID_MAX_LENGTH, resourceIdProblem } from "@/lib/memory";
import { useMediaQuery } from "@/components/frame/useMediaQuery";
import { Button } from "@/components/ui/Button";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/Popover";
import { Sheet, SheetBody, SheetContent, SheetHeader } from "@/components/ui/Sheet";
import { CONTEXT_SHEET_QUERY } from "@/components/requestContext/RequestContextPopover";

export const ACT_AS_TITLE = "Act as user";
export const ACT_AS_PANEL_ID = "composer-act-as-panel";

export interface ComposerActAsChipProps {
  /** The user id new conversations act for; "" for the default (the API uses your user id). */
  value: string;
  onChange: (resourceId: string) => void;
  /**
   * The open conversation's user: its resource id is fixed when it is created, so the chip shows it
   * and the choice applies only to the next new conversation. undefined: a new conversation.
   */
  conversationUser?: string | null;
  disabled?: boolean;
}

/**
 * The chip's text: "You", the id, or the open conversation's user, followed by the choice waiting for
 * the next new conversation when it is another user ("alice (next: bob)").
 */
export function actAsLabel(value: string, conversationUser: string | null | undefined): string {
  if (conversationUser === undefined) return value || "You";
  const current = conversationUser ?? "No user";
  return value && value !== conversationUser ? `${current} (next: ${value})` : current;
}

const chipClass =
  "inline-flex h-7 min-w-0 max-w-[11rem] shrink items-center gap-1 rounded-full border border-border bg-surface px-2 text-label text-foreground hover:bg-fill-hover disabled:cursor-not-allowed disabled:opacity-50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background data-[state=open]:bg-fill-selected";

/**
 * The composer's third chip (RM.18), shown for an agent with memory: the user (`resource_id`) a NEW
 * conversation acts for, so per-user working memory and recall can be tried as several users. A
 * conversation's user never changes: while one is open, the chip shows its user and the choice
 * waits for the next new conversation. Free text, checked as the API checks it
 * (`resourceIdProblem`). A popover, a Sheet below 640 px (layout rule 7).
 */
export function ComposerActAsChip({ value, onChange, conversationUser, disabled = false }: ComposerActAsChipProps) {
  const [open, setOpen] = useState(false);
  const [draft, setDraft] = useState(value);
  const sheet = useMediaQuery(CONTEXT_SHEET_QUERY, false);
  const inputId = useId();
  const errorId = useId();
  useEffect(() => {
    if (open) setDraft(value);
  }, [open, value]);

  const trimmed = draft.trim();
  const problem = trimmed ? resourceIdProblem(trimmed) : null;
  const label = actAsLabel(value, conversationUser);
  const openConversation = conversationUser !== undefined;

  const apply = (next: string) => {
    onChange(next);
    setOpen(false);
  };

  const body = (
    <form
      className="min-w-0 space-y-3"
      onSubmit={(event) => {
        event.preventDefault();
        if (!problem) apply(trimmed);
      }}
    >
      <p className="text-caption text-muted-foreground">
        New conversations act for this user: per-user working memory and recall are theirs. Leave it empty to act as yourself.
      </p>
      {openConversation ? (
        <p data-testid="act-as-fixed" className="text-caption text-muted-foreground">
          This conversation acts for <span className="font-mono text-foreground">{conversationUser ?? "no user"}</span> and keeps it; the choice applies to the next new
          conversation.
        </p>
      ) : null}
      <div className="space-y-1">
        <label htmlFor={inputId} className="text-label font-medium text-foreground">
          User id
        </label>
        <input
          id={inputId}
          data-testid="act-as-input"
          value={draft}
          maxLength={RESOURCE_ID_MAX_LENGTH}
          autoComplete="off"
          spellCheck={false}
          placeholder="alice@example.com"
          onChange={(event) => setDraft(event.target.value)}
          aria-invalid={Boolean(problem)}
          aria-describedby={problem ? errorId : undefined}
          className="h-8 w-full min-w-0 rounded-md border border-input bg-surface px-2 font-mono text-label text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        />
        {problem ? (
          <p id={errorId} role="alert" data-testid="act-as-error" className="text-caption text-destructive-text">
            {problem}
          </p>
        ) : null}
      </div>
      <div className="flex flex-wrap justify-end gap-2">
        {value ? (
          <Button size="sm" variant="ghost" onClick={() => apply("")} data-testid="act-as-clear">
            Act as yourself
          </Button>
        ) : null}
        <Button size="sm" variant="primary" type="submit" disabled={Boolean(problem)} data-testid="act-as-apply">
          Apply
        </Button>
      </div>
    </form>
  );

  const chip = (props: React.ButtonHTMLAttributes<HTMLButtonElement> & { "data-state"?: string }) => (
    <button
      type="button"
      data-testid="composer-chip-act-as"
      aria-label={`${ACT_AS_TITLE}: ${label}`}
      aria-controls={open ? ACT_AS_PANEL_ID : undefined}
      disabled={disabled}
      className={cn(chipClass)}
      {...props}
    >
      <UserRound aria-hidden="true" strokeWidth={ICON_STROKE_WIDTH} className="size-3.5 shrink-0 text-muted-foreground" />
      <span aria-hidden="true" data-testid="composer-chip-label" className="min-w-0 truncate">
        {label}
      </span>
      <ChevronDown aria-hidden="true" strokeWidth={ICON_STROKE_WIDTH} className="size-3.5 shrink-0 text-muted-foreground" />
    </button>
  );

  if (sheet) {
    return (
      <>
        {chip({ "aria-haspopup": "dialog", "aria-expanded": open, "data-state": open ? "open" : "closed", onClick: () => setOpen(!open) })}
        <Sheet open={open} onOpenChange={setOpen}>
          <SheetContent id={ACT_AS_PANEL_ID} size="md" aria-describedby={undefined} data-testid="act-as-sheet">
            <SheetHeader title={ACT_AS_TITLE} />
            <SheetBody>{body}</SheetBody>
          </SheetContent>
        </Sheet>
      </>
    );
  }

  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger asChild>{chip({})}</PopoverTrigger>
      <PopoverContent id={ACT_AS_PANEL_ID} aria-label={ACT_AS_TITLE} data-testid="act-as-popover" side="top" align="start" className="w-80">
        <h2 className="mb-2 text-title font-semibold text-foreground">{ACT_AS_TITLE}</h2>
        {body}
      </PopoverContent>
    </Popover>
  );
}
