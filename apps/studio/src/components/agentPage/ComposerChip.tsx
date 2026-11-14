"use client";

import React from "react";
import { ChevronDown } from "lucide-react";
import { cn } from "@/lib/cn";
import { ICON_STROKE_WIDTH } from "@/lib/icons";

export interface ComposerChipSelectProps extends Omit<React.SelectHTMLAttributes<HTMLSelectElement>, "children"> {
  id: string;
  /** The chip's test id. */
  testId: string;
  /** The select's accessible name ("Version", "Model"). */
  label: string;
  /** What the chip shows: the current choice, short ("v3", "Draft", "gpt-4.1-mini"). */
  display: string;
  icon: React.ReactNode;
  invalid?: boolean;
  /** Error element id, when the choice was refused. */
  errorId?: string;
  children: React.ReactNode;
}

/**
 * A composer chip that opens a menu: a compact pill showing the current choice, over a native
 * `<select>` that takes the clicks and the keyboard (on a phone it opens the system picker). The
 * select is the control (its label and value are what assistive technology reads); the pill is drawn
 * for it. The chip truncates its text so the chips row fits a 375 px screen.
 */
export function ComposerChipSelect({ id, testId, label, display, icon, invalid = false, errorId, className, disabled, children, ...props }: ComposerChipSelectProps) {
  return (
    <span
      data-testid={testId}
      className={cn(
        "relative inline-flex h-7 min-w-0 max-w-[11rem] shrink items-center gap-1 rounded-full border px-2 text-label",
        "focus-within:ring-2 focus-within:ring-ring focus-within:ring-offset-2 focus-within:ring-offset-background",
        invalid ? "border-destructive/60 bg-destructive/10 text-destructive-text" : "border-border bg-surface text-foreground hover:bg-fill-hover",
        disabled && "opacity-50",
        className,
      )}
    >
      <span aria-hidden="true" className="flex shrink-0 text-muted-foreground [&_svg]:size-3.5">
        {icon}
      </span>
      <span aria-hidden="true" data-testid="composer-chip-label" className="min-w-0 truncate">
        {display}
      </span>
      <ChevronDown aria-hidden="true" strokeWidth={ICON_STROKE_WIDTH} className="size-3.5 shrink-0 text-muted-foreground" />
      <select
        id={id}
        aria-label={label}
        aria-invalid={invalid}
        aria-describedby={invalid && errorId ? errorId : undefined}
        disabled={disabled}
        className="absolute inset-0 h-full w-full cursor-pointer appearance-none rounded-full opacity-0 outline-none disabled:cursor-not-allowed"
        {...props}
      >
        {children}
      </select>
    </span>
  );
}

/** A refusal shown under the chips, linked from its chip by `aria-describedby`. */
export function ComposerFieldError({ id, message }: { id: string; message: string | null }) {
  if (!message) return null;
  return (
    <p id={id} role="alert" className="text-caption text-destructive-text">
      {message}
    </p>
  );
}
