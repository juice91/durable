"use client";

import Link from "next/link";
import React, { createContext, useContext } from "react";
import { routes } from "@/lib/routes";
import type { TraceTab } from "@/lib/routes/observability";

/** What a page hosting the trace Sheet offers (`TraceSheetHost`). */
export interface TraceSheetApi {
  /** The run whose trace Sheet is open (from the URL), or null. */
  runId: string | null;
  /** Open (or switch) the trace Sheet to `runId`, on `tab` (default Spans). */
  open: (runId: string, tab?: TraceTab | null) => void;
  /** Stack a second trace Sheet over the first (a child run); a third replaces the second. */
  openChild: (runId: string, tab?: TraceTab | null) => void;
  close: () => void;
  /** The hosting list's run ids, in order, for prev / next (a nested host registers its own). */
  setRunIds: (runIds: readonly string[]) => void;
}

export const TraceSheetContext = createContext<TraceSheetApi | null>(null);

/** What a trace Sheet tells the links inside it. */
export interface TraceSheetFrame {
  /** 0: the Sheet from the URL; 1: a stacked child run. */
  depth: number;
  /** The run this Sheet shows. */
  runId: string;
  /** Switch this Sheet's tab (a link to the same run switches tabs instead of stacking a copy). */
  onTabChange: (tab: TraceTab) => void;
}

/** Set inside a trace Sheet; null elsewhere. */
export const TraceSheetFrameContext = createContext<TraceSheetFrame | null>(null);

/** The hosting page's trace Sheet, or null when the page hosts none. */
export function useTraceSheet(): TraceSheetApi | null {
  return useContext(TraceSheetContext);
}

export interface TraceLinkProps extends Omit<React.AnchorHTMLAttributes<HTMLAnchorElement>, "href"> {
  runId: string;
  tab?: TraceTab | null;
}

/** A modified click (new tab or window, download) follows the href instead of opening in place. */
function isPlainClick(event: React.MouseEvent<HTMLAnchorElement>): boolean {
  return event.button === 0 && !event.metaKey && !event.ctrlKey && !event.shiftKey && !event.altKey;
}

/**
 * A link to a run's trace, `routes.trace(runId, {tab})`. On a page that hosts the trace Sheet a
 * plain click opens it in place: from the list, the Sheet itself; from inside a trace Sheet, a
 * second Sheet stacked on top (child runs, the parent run), or, for the Sheet's own run, that
 * Sheet's tab ("Run details" on the root span). Elsewhere it goes to `/traces?trace=`.
 */
export const TraceLink = React.forwardRef<HTMLAnchorElement, TraceLinkProps>(function TraceLink({ runId, tab = null, onClick, children, ...props }, ref) {
  const host = useTraceSheet();
  const frame = useContext(TraceSheetFrameContext);
  return (
    <Link
      ref={ref}
      href={routes.trace(runId, { tab })}
      onClick={(event) => {
        onClick?.(event);
        if (event.defaultPrevented || !isPlainClick(event)) return;
        if (frame && frame.runId === runId) {
          event.preventDefault();
          frame.onTabChange(tab ?? "spans");
          return;
        }
        if (!host) return;
        event.preventDefault();
        if (frame === null) host.open(runId, tab);
        else host.openChild(runId, tab);
      }}
      {...props}
    >
      {children}
    </Link>
  );
});
