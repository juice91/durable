"use client";

import React from "react";
import { ComposerPrimitive } from "@assistant-ui/react";
import { ArrowUp, ShieldCheck } from "lucide-react";
import { cn } from "@/lib/cn";
import { ICON_STROKE_WIDTH } from "@/lib/icons";
import { focusRing } from "@/components/ui/Button";
import { AttachButton, ComposerAttachmentList } from "@/components/chat/Attachments";

export interface ComposerProps {
  /** The run option chips (version, model) and the request context button, in that order. */
  chips: React.ReactNode;
  /** Shows the "Output checked before display" hint: the target has output processors (P0.17). */
  outputChecked: boolean;
  /** Refusals and notices about the options, under the chips. */
  notices?: React.ReactNode;
  placeholder?: string;
  /** The runtime has an attachment adapter (the AG-UI chat): shows the paperclip and the file chips. */
  attachments?: boolean;
}

/**
 * The chat composer (inside the thread's assistant-ui runtime): the message box, then a row with the
 * option chips on the left and the output-check hint and Send on the right. Enter sends, Shift+Enter
 * adds a line. The row wraps, so the chips fit a 375 px screen.
 */
export function Composer({ chips, outputChecked, notices, placeholder = "Message the agent…", attachments = false }: ComposerProps) {
  return (
    <ComposerPrimitive.Root
      data-testid="chat-composer"
      className="flex min-w-0 flex-col gap-2 rounded-lg border border-border bg-surface-raised p-2 shadow-raised focus-within:border-ring"
    >
      {attachments ? <ComposerAttachmentList /> : null}
      <ComposerPrimitive.Input
        aria-label="Message"
        placeholder={placeholder}
        rows={2}
        className="max-h-60 min-h-12 w-full min-w-0 resize-none bg-transparent px-2 py-1.5 text-body text-foreground placeholder:text-muted-foreground focus-visible:outline-none disabled:cursor-not-allowed disabled:opacity-60"
      />
      <div className="flex min-w-0 flex-wrap items-center gap-1.5" data-testid="run-options">
        {attachments ? <AttachButton /> : null}
        {chips}
        <div className="ml-auto flex shrink-0 items-center gap-2">
          {outputChecked ? (
            <span data-testid="composer-output-checked" className="inline-flex items-center gap-1 text-caption text-muted-foreground">
              <ShieldCheck aria-hidden="true" strokeWidth={ICON_STROKE_WIDTH} className="size-3.5" />
              <span className="hidden sm:inline">Output checked before display</span>
              <span className="sr-only sm:hidden">Output checked before display</span>
            </span>
          ) : null}
          <ComposerPrimitive.Send
            aria-label="Send message"
            className={cn(
              "inline-flex size-8 items-center justify-center rounded-md bg-primary text-primary-foreground hover:bg-primary/90 disabled:cursor-not-allowed disabled:opacity-50",
              focusRing,
            )}
          >
            <ArrowUp aria-hidden="true" strokeWidth={ICON_STROKE_WIDTH} className="size-icon" />
          </ComposerPrimitive.Send>
        </div>
      </div>
      {notices ? <div className="min-w-0 space-y-1 px-1">{notices}</div> : null}
    </ComposerPrimitive.Root>
  );
}
