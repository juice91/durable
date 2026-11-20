"use client";

import React from "react";

export interface ShowSkippedToggleProps {
  show: boolean;
  onChange: (next: boolean) => void;
  /** How many skipped runs the loaded list holds. */
  count: number;
}

/**
 * Run lists hide skipped runs (a "nothing to do" exit) by default; this lists them. Shown only
 * when the loaded runs hold a skipped one, or while they are shown (so they can be hidden again).
 */
export function ShowSkippedToggle({ show, onChange, count }: ShowSkippedToggleProps) {
  if (count === 0 && !show) return null;
  return (
    <button
      type="button"
      data-testid="show-skipped-toggle"
      aria-pressed={show}
      onClick={() => onChange(!show)}
      className="rounded-md border border-border px-2 py-0.5 text-caption text-foreground outline-none hover:bg-fill-hover focus-visible:ring-2 focus-visible:ring-ring"
    >
      {show ? `Hide skipped (${count})` : `Show skipped (${count})`}
    </button>
  );
}
