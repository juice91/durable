"use client";

import React, { useCallback, useState } from "react";
import { Group, Panel, Separator, type Layout, type LayoutChangedMeta } from "react-resizable-panels";
import { cn } from "@/lib/cn";
import { readStorage, writeStorage } from "@/lib/safeStorage";

/** Reads a saved layout (`{panelId: percent}`); null when absent, unreadable or malformed. */
export function readStoredLayout(storageKey: string): Layout | null {
  try {
    const raw = readStorage(storageKey);
    if (!raw) return null;
    const parsed: unknown = JSON.parse(raw);
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) return null;
    const entries = Object.entries(parsed as Record<string, unknown>);
    if (entries.length === 0 || !entries.every(([, size]) => typeof size === "number" && Number.isFinite(size) && size >= 0 && size <= 100)) return null;
    return Object.fromEntries(entries) as Layout;
  } catch {
    return null;
  }
}

/** Saves a layout; storage errors (private mode, quota) are ignored. */
export function writeStoredLayout(storageKey: string, layout: Layout): void {
  // Not saved when storage is unavailable or full: the panes still work, they reopen at their defaults.
  writeStorage(storageKey, JSON.stringify(layout));
}

export interface ResizableGroupProps extends Omit<React.ComponentProps<typeof Group>, "defaultLayout"> {
  /** localStorage key; when set, sizes are restored on mount and saved after each resize. */
  storageKey?: string;
  /** Used when nothing is stored. Keys are Panel ids. */
  defaultLayout?: Layout;
}

/**
 * Resizable panes. Give every `ResizablePanel` a stable `id`: saved layouts are keyed by it. Separators
 * are keyboard-operable (arrow keys, Home, End) and need a label.
 */
export function ResizableGroup({ storageKey, defaultLayout, onLayoutChanged, className, ...props }: ResizableGroupProps) {
  // Read once on mount: Group applies defaultLayout only when it mounts.
  const [initial] = useState<Layout | undefined>(() => (storageKey && typeof window !== "undefined" ? readStoredLayout(storageKey) : null) ?? defaultLayout);
  const handleLayoutChanged = useCallback(
    (layout: Layout, meta: LayoutChangedMeta) => {
      if (storageKey && meta.isUserInteraction) writeStoredLayout(storageKey, meta.requestedLayout ?? layout);
      onLayoutChanged?.(layout, meta);
    },
    [onLayoutChanged, storageKey],
  );
  return <Group defaultLayout={initial} onLayoutChanged={handleLayoutChanged} className={cn("min-h-0 min-w-0", className)} {...props} />;
}

export const ResizablePanel = Panel;

export interface ResizableHandleProps extends Omit<React.ComponentProps<typeof Separator>, "aria-label"> {
  /** Accessible name ("Resize threads panel"). Required. */
  label: string;
}

/** The drag and keyboard handle between two panels: a 1 px line with a wider hit area. */
export function ResizableHandle({ label, className, ...props }: ResizableHandleProps) {
  return (
    <Separator
      aria-label={label}
      className={cn(
        "relative flex w-px shrink-0 items-center justify-center bg-border outline-none transition-colors motion-reduce:transition-none",
        "hover:bg-border-strong focus-visible:bg-ring data-[separator=active]:bg-ring",
        "aria-[orientation=horizontal]:h-px aria-[orientation=horizontal]:w-full",
        className,
      )}
      {...props}
    />
  );
}
