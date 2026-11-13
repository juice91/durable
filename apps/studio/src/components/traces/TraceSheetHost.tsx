"use client";

import React, { useCallback, useContext, useEffect, useMemo, useRef, useState } from "react";
import { useSearchParams } from "next/navigation";
import { useUrlSheet } from "@/components/ui/useUrlSheet";
import { parseTraceTab, TRACE_PARAM, TRACE_TAB_PARAM, type TraceTab } from "@/lib/routes/observability";
import { TraceSheetContext, type TraceSheetApi } from "./TraceLink";
import { TraceSheet } from "./TraceSheet";
import { prevNext } from "./traceSheetModel";

/**
 * `params` (a page's own filters) plus the open trace Sheet's parameters from `current` (the URL):
 * a page rewriting its filters keeps the Sheet open on the same tab.
 */
export function withSheetParams(
  params: URLSearchParams,
  current: { get(name: string): string | null } | null | undefined,
  tabParam: string = TRACE_TAB_PARAM,
): URLSearchParams {
  const out = new URLSearchParams(params);
  for (const key of [TRACE_PARAM, tabParam]) {
    const value = current?.get(key);
    if (value) out.set(key, value);
    else out.delete(key);
  }
  return out;
}

export interface TraceSheetHostProps {
  /** The hosting list's run ids in display order, for prev / next. */
  runIds?: readonly string[];
  /**
   * The query parameter for the Sheet's tab. `tab` by default (`/traces?trace=R&tab=events`); a page
   * whose own tabs already use `tab` (a workflow's `?tab=runs`) passes another name.
   */
  tabParam?: string;
  children?: React.ReactNode | ((sheet: TraceSheetApi) => React.ReactNode);
}

/**
 * Makes the page a host of the trace Sheet (SL.12): `?trace=<runId>` (and the tab parameter) opens
 * it, so the open trace survives a reload and is shareable; `TraceLink`s below open it in place, and
 * trace links inside it stack a second Sheet (a child run). At most one host renders the Sheet: a
 * host inside another only hands its list's run ids to the outer one for prev / next.
 *
 * History, as the tool Sheet (SL.13): opening pushes one entry, so Back closes the Sheet; prev,
 * next and tab changes replace it; closing a Sheet this host opened goes Back (Back never reopens
 * it). A Sheet that came with the URL (a reload, a shared link) closes by replacing.
 *
 * Focus: closing returns to whatever opened the Sheet; after prev or next, to the list row of the
 * run then shown (DataList's selected row), when that row is on the page.
 *
 * Uses `useSearchParams`: in the Next app router the page must sit inside a `<Suspense>` boundary.
 */
export function TraceSheetHost(props: TraceSheetHostProps) {
  const outer = useContext(TraceSheetContext);
  return outer ? <NestedHost outer={outer} {...props} /> : <RootHost {...props} />;
}

function render(children: TraceSheetHostProps["children"], api: TraceSheetApi): React.ReactNode {
  return typeof children === "function" ? children(api) : children;
}

function NestedHost({ outer, runIds, children }: TraceSheetHostProps & { outer: TraceSheetApi }) {
  const key = runIds ? runIds.join("\n") : null;
  useEffect(() => {
    if (key === null) return;
    outer.setRunIds(key ? key.split("\n") : []);
    return () => outer.setRunIds([]);
    // `key` stands for runIds' content; `outer.setRunIds` is a stable state setter.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key, outer.setRunIds]);
  return <>{render(children, outer)}</>;
}

interface ChildSheet {
  runId: string;
  tab: TraceTab;
}

function RootHost({ runIds, tabParam = TRACE_TAB_PARAM, children }: TraceSheetHostProps) {
  const pushSheet = useUrlSheet(TRACE_PARAM, { clear: [tabParam], history: "push" });
  const replaceSheet = useUrlSheet(TRACE_PARAM, { clear: [tabParam] });
  const searchParams = useSearchParams();
  const tab = parseTraceTab(searchParams?.get(tabParam));
  const [registered, setRegistered] = useState<readonly string[]>([]);
  const [child, setChild] = useState<ChildSheet | null>(null);
  const value = pushSheet.value;
  const { open: pushOpen, close } = pushSheet;
  const { open: replaceOpen } = replaceSheet;
  // True while the Sheet on screen was opened by this host's push (its entry is on top).
  const pushed = useRef(false);
  // The run the Sheet was opened on; a different one on close means prev / next moved it. Kept
  // after the Sheet closes: its focus return runs after this host has seen the URL drop it.
  const openedRunId = useRef<string | null>(null);
  const openedByHost = useRef(false);
  const previousValue = useRef<string | null>(null);

  useEffect(() => {
    // A child Sheet belongs to the trace it was opened from.
    setChild(null);
    if (value === null) pushed.current = false;
    // A Sheet that appears from the URL (a reload, a link, Back) was opened on its own run.
    else if (previousValue.current === null && !openedByHost.current) openedRunId.current = value;
    openedByHost.current = false;
    previousValue.current = value;
  }, [value]);

  const tabExtra = useCallback((nextTab: TraceTab | null) => ({ [tabParam]: nextTab && nextTab !== "spans" ? nextTab : null }), [tabParam]);

  /** Open (or switch to) a run from a list or a link: one history entry. */
  const open = useCallback(
    (runId: string, nextTab: TraceTab | null = null) => {
      pushOpen(runId, tabExtra(nextTab));
      pushed.current = true;
      openedRunId.current = runId;
      openedByHost.current = true;
    },
    [pushOpen, tabExtra],
  );
  /** Prev / next and tab changes: replace the current entry. */
  const navigate = useCallback(
    (runId: string, nextTab: TraceTab | null) => {
      // Through the push hook while its entry is on top (it replaces then, and closing still goes Back).
      if (pushed.current) pushOpen(runId, tabExtra(nextTab));
      else replaceOpen(runId, tabExtra(nextTab));
    },
    [pushOpen, replaceOpen, tabExtra],
  );
  const onTabChange = useCallback((next: TraceTab) => {
    if (value) navigate(value, next);
  }, [navigate, value]);
  const closeFocusTarget = useCallback((runId: string): HTMLElement | null => {
    if (openedRunId.current === null || runId === openedRunId.current || typeof document === "undefined") return null;
    const row = Array.from(document.querySelectorAll<HTMLElement>("[data-row-id]")).find((el) => el.dataset.rowId === runId);
    return row?.querySelector<HTMLElement>("[data-datalist-primary]") ?? null;
  }, []);
  const openChild = useCallback((runId: string, nextTab: TraceTab | null = null) => setChild({ runId, tab: nextTab ?? "spans" }), []);

  const api = useMemo<TraceSheetApi>(
    () => ({ runId: value, open, openChild, close, setRunIds: setRegistered }),
    [value, open, openChild, close],
  );

  const ids = registered.length ? registered : (runIds ?? []);
  const nav = prevNext(ids, value);
  const navigation =
    nav.position !== null
      ? { prevId: nav.prev, nextId: nav.next, position: nav.position, total: nav.total, onNavigate: (id: string) => navigate(id, tab) }
      : null;

  return (
    <TraceSheetContext.Provider value={api}>
      {render(children, api)}
      {value ? (
        <TraceSheet
          runId={value}
          open
          onOpenChange={pushSheet.onOpenChange}
          tab={tab}
          onTabChange={onTabChange}
          navigation={navigation}
          closeFocusTarget={closeFocusTarget}
        />
      ) : null}
      {value && child ? (
        <TraceSheet
          runId={child.runId}
          open
          depth={1}
          onOpenChange={(isOpen) => {
            if (!isOpen) setChild(null);
          }}
          tab={child.tab}
          onTabChange={(next) => setChild((current) => (current ? { ...current, tab: next } : current))}
        />
      ) : null}
    </TraceSheetContext.Provider>
  );
}
