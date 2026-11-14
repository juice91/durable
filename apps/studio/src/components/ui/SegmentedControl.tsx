"use client";

import React from "react";
import * as ToggleGroupPrimitive from "@radix-ui/react-toggle-group";
import { cn } from "@/lib/cn";
import { focusRing } from "./Button";

export interface SegmentedOption<T extends string = string> {
  value: T;
  label: React.ReactNode;
  icon?: React.ReactNode;
  /** Accessible name when `label` is visually hidden or not text. */
  ariaLabel?: string;
  disabled?: boolean;
}

export interface SegmentedControlProps<T extends string = string> {
  /** Accessible name of the group ("Theme"). */
  label: string;
  options: SegmentedOption<T>[];
  value: T;
  onValueChange: (value: T) => void;
  size?: "sm" | "md";
  className?: string;
}

/**
 * A single-choice row of toggle buttons (System / Light / Dark, List / Compact). Arrow keys move
 * between options; one option is always selected (clicking the active one does nothing).
 */
export function SegmentedControl<T extends string = string>({ label, options, value, onValueChange, size = "md", className }: SegmentedControlProps<T>) {
  return (
    <ToggleGroupPrimitive.Root
      type="single"
      aria-label={label}
      value={value}
      onValueChange={(next) => {
        if (next) onValueChange(next as T);
      }}
      className={cn("inline-flex max-w-full items-center gap-0.5 rounded-md border border-border bg-muted p-0.5", className)}
    >
      {options.map((option) => (
        <ToggleGroupPrimitive.Item
          key={option.value}
          value={option.value}
          aria-label={option.ariaLabel}
          disabled={option.disabled}
          // Enter and Space select the focused option explicitly (not only through the button's
          // native click), so keyboard selection does not depend on the platform's activation.
          onKeyDown={(event) => {
            if (event.key !== "Enter" && event.key !== " ") return;
            event.preventDefault();
            if (!option.disabled && option.value !== value) onValueChange(option.value);
          }}
          className={cn(
            "inline-flex min-w-0 items-center justify-center gap-1.5 rounded-sm px-2 font-medium text-muted-foreground transition-colors motion-reduce:transition-none hover:text-foreground [&_svg]:size-icon [&_svg]:shrink-0",
            "data-[state=on]:bg-surface data-[state=on]:text-foreground data-[state=on]:shadow-raised disabled:pointer-events-none disabled:opacity-50",
            size === "sm" ? "h-6 text-caption" : "h-7 text-label",
            focusRing,
          )}
        >
          {option.icon}
          {option.label}
        </ToggleGroupPrimitive.Item>
      ))}
    </ToggleGroupPrimitive.Root>
  );
}
