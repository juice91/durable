"use client";

import React, { useCallback, useEffect, useRef, useState } from "react";
import { useGroupRef, usePanelRef, type Layout, type LayoutChangedMeta } from "react-resizable-panels";
import { ResizableGroup, ResizableHandle, ResizablePanel, readStoredLayout, writeStoredLayout } from "@/components/ui/Resizable";
import { Sheet, SheetBody, SheetContent, SheetHeader } from "@/components/ui/Sheet";
import { useIsWide } from "@/components/frame/useMediaQuery";
import { removeStorage } from "@/lib/safeStorage";
import { agentLayoutKey, readThreadsWidth, writeThreadsWidth } from "./agentPageModel";
import { useAgentPage } from "./AgentPageContext";
import { CHAT_MIN_PX, clampThreadsWidth, paneWidthFromLayout, THREADS_DEFAULT_PX, THREADS_MIN_PX, threadsShown } from "./chatFit";

// The pane minimums live in chatFit.ts (one source for the panes and the fit decisions).
export { CHAT_MIN_PX, THREADS_DEFAULT_PX, THREADS_MIN_PX };

export const THREADS_TITLE = "Conversations";

function clearStoredLayout(storageKey: string): void {
  // Storage unavailable: nothing was saved either.
  removeStorage(storageKey);
}

/** True after the first client render (resizable panes render client-only: their sizes come from
 * storage and the window, which the server cannot know, so server markup would not hydrate). */
function useMounted(): boolean {
  const [mounted, setMounted] = useState(false);
  useEffect(() => setMounted(true), []);
  return mounted;
}

export interface AgentChatPanesProps {
  agentId: string;
  /** The threads pane: the conversation list and the memory card. */
  threads: React.ReactNode;
  /** The chat. */
  children: React.ReactNode;
}

/** The total width of the group's own resize handles (the part of the group no pane gets). */
function separatorsWidth(element: HTMLElement | null | undefined): number {
  if (!element) return 0;
  return Array.from(element.querySelectorAll<HTMLElement>(":scope > [data-separator]")).reduce((sum, separator) => sum + separator.getBoundingClientRect().width, 0);
}

/**
 * The Chat tab's panes (decision "Agent chat panes"). From 1024 px: threads | chat, resizable, sizes
 * saved per agent in `localStorage["studio.agent-layout.v1.{id}"]`, the threads pane collapsible
 * with `{` or the header's Conversations button. Below 1024 px the chat takes the full width and the
 * threads open as a Sheet. The Config pane is the frame's side panel (registered by `AgentLayout`).
 * While Config leaves no room for both panes, the page marks the threads auto-collapsed: the pane
 * collapses without saving, and comes back (unless the user closed it) when the room returns.
 */
export function AgentChatPanes({ agentId, threads, children }: AgentChatPanesProps) {
  const page = useAgentPage();
  const isWide = useIsWide();
  const mounted = useMounted();
  const [localOpen, setLocalOpen] = useState(true);
  const threadsOpen = page ? page.threadsOpen : localOpen;
  const setThreadsOpen = page ? page.setThreadsOpen : setLocalOpen;
  const autoCollapsed = page?.threadsAutoCollapsed ?? false;
  // What the pane shows; `threadsOpen` is the user's choice, the only thing saved.
  const shown = threadsShown(threadsOpen, autoCollapsed);
  const panelRef = usePanelRef();
  const groupRef = useGroupRef();
  const [groupElement, setGroupElement] = useState<HTMLDivElement | null>(null);
  const storageKey = agentLayoutKey(agentId);
  const reportPane = page?.reportThreadsPane;
  // Read through refs: the kit and the ResizeObserver call back outside React's render cycle.
  const shownRef = useRef(shown);
  shownRef.current = shown;
  const reportRef = useRef(reportPane);
  reportRef.current = reportPane;

  // The first time the panes exist, the saved layout decides whether threads start collapsed; after
  // that, the toggle (and the auto-collapse) drives the pane.
  const adopted = useRef(false);
  // A user's toggle waiting to be saved: saved only once the pane really shows it, so a refused
  // expand is never stored as a collapse.
  const pendingSave = useRef<boolean | null>(null);

  /**
   * Saves the user's open or closed choice, and only that: a toggle never writes widths (the layout
   * right after an expand is transient: the kit's leftover percent, a group still resizing). Widths
   * are saved by real drags only (the kit saves the layout, `onLayoutChanged` the px width).
   */
  const saveUserLayout = useCallback(
    (open: boolean) => {
      if (!open) writeStoredLayout(storageKey, { threads: 0, chat: 100 });
      // Open: forget a saved collapse; a saved dragged layout stays as it is.
      else if (readStoredLayout(storageKey)?.threads === 0) clearStoredLayout(storageKey);
    },
    [storageKey],
  );

  // The width an expanded pane keeps, in px: the user's last dragged width, else the default.
  const threadsWidth = useRef<number>(THREADS_DEFAULT_PX);
  useEffect(() => {
    threadsWidth.current = readThreadsWidth(agentId) ?? THREADS_DEFAULT_PX;
  }, [agentId]);
  const groupElementRef = useRef<HTMLDivElement | null>(null);
  groupElementRef.current = groupElement;

  /**
   * Keeps an expanded pane at the user's width in px. The kit sizes panes in percent of the group,
   * and an expand restores the percent it remembered from before the collapse, measured while the
   * group may have been much narrower or wider (Config open, the sidebar moving): that is how a
   * reopened pane came back at the separator's maximum. Skipped while the group has no size (jsdom,
   * before layout).
   */
  const pinWidth = useCallback(() => {
    const panel = panelRef.current;
    const groupWidth = groupElementRef.current?.getBoundingClientRect().width ?? 0;
    if (!panel || panel.isCollapsed() || groupWidth <= 0) return;
    const target = threadsWidth.current;
    if (Math.abs(panel.getSize().inPixels - target) > 1) panel.resize(target);
  }, [panelRef]);

  /**
   * Makes the pane match `shown`, and reports the pane's real state to the header. An expand can be
   * refused: while the group is narrower than threads + chat minimums (mid-way through the sidebar's
   * width transition, say) the kit snaps the pane back to collapsed. So this runs again whenever the
   * group's size changes, until the pane really matches. A pending user toggle is saved here, once
   * the pane shows it.
   */
  const syncPane = useCallback(() => {
    const panel = panelRef.current;
    if (!panel || !adopted.current) return;
    const collapsed = panel.isCollapsed();
    if (shownRef.current && collapsed) panel.expand();
    else if (!shownRef.current && !collapsed) panel.collapse();
    if (shownRef.current) pinWidth();
    const expanded = !panel.isCollapsed();
    reportRef.current?.(expanded);
    if (pendingSave.current !== null && pendingSave.current === expanded) {
      saveUserLayout(expanded);
      pendingSave.current = null;
    }
  }, [panelRef, pinWidth, saveUserLayout]);

  // Retry after the group resizes (the window, the sidebar, the side panel), a frame later so the
  // kit has applied the group's new size first.
  useEffect(() => {
    if (!groupElement || typeof ResizeObserver === "undefined") return;
    let frame: number | null = null;
    const schedule =
      typeof window.requestAnimationFrame === "function"
        ? (run: () => void) => window.requestAnimationFrame(run)
        : (run: () => void) => window.setTimeout(run, 0);
    const cancel = (id: number) =>
      typeof window.cancelAnimationFrame === "function" ? window.cancelAnimationFrame(id) : window.clearTimeout(id);
    const observer = new ResizeObserver(() => {
      if (frame !== null) cancel(frame);
      frame = schedule(() => {
        frame = null;
        syncPane();
      });
    });
    observer.observe(groupElement);
    return () => {
      observer.disconnect();
      if (frame !== null) cancel(frame);
    };
  }, [groupElement, syncPane]);

  // The header shows the pane's real state only while the wide panes exist.
  useEffect(() => {
    if (!isWide || !mounted) return;
    return () => reportRef.current?.(null);
  }, [isWide, mounted]);

  /**
   * A layout saved before px widths were kept (a drag's percent layout, no `:threads-px`): once the
   * group has a size, its threads width becomes the user's width and is saved, so the next visit (at
   * another window width) and a later collapse (which overwrites the percent layout) both keep it.
   * Computed from the stored percent, as a drag's is, and clamped to a usable width (the threads
   * minimum; room for the chat's minimum). Waits for the group element (it arrives one render after
   * the panes adopt the saved layout) and, on a first visit with threads auto-collapsed (Config
   * crowding them out), until the pane is shown: the group's width then is the one that counts.
   */
  const migratedFor = useRef<string | null>(null);
  useEffect(() => {
    if (!isWide || !mounted || !groupElement || !shown || migratedFor.current === agentId) return;
    migratedFor.current = agentId;
    if (readThreadsWidth(agentId) !== null) return;
    const storedPercent = readStoredLayout(storageKey)?.threads;
    if (typeof storedPercent !== "number" || storedPercent <= 0) return;
    const groupWidth = groupElement.getBoundingClientRect().width;
    const separators = separatorsWidth(groupElement);
    const width = paneWidthFromLayout(storedPercent, groupWidth, separators);
    if (width === null || width <= 0) {
      migratedFor.current = null; // no size yet (before layout): tried again when an input changes
      return;
    }
    const clamped = clampThreadsWidth(width, groupWidth, separators);
    threadsWidth.current = clamped;
    writeThreadsWidth(agentId, clamped);
  }, [agentId, groupElement, isWide, mounted, shown, storageKey]);

  const lastUserOpen = useRef(threadsOpen);
  useEffect(() => {
    if (!isWide || !mounted) {
      adopted.current = false;
      return;
    }
    const panel = panelRef.current;
    if (!panel) return;
    if (!adopted.current) {
      adopted.current = true;
      const savedOpen = !panel.isCollapsed();
      // (A percent layout from before px widths is migrated by the effect above, once the group has a size.)
      lastUserOpen.current = savedOpen;
      if (savedOpen !== threadsOpen) {
        setThreadsOpen(savedOpen);
        return;
      }
    }
    // Only the user's choice is saved (by syncPane, once the pane shows it); an auto-collapse, or
    // its undoing, is not.
    if (lastUserOpen.current !== threadsOpen) pendingSave.current = threadsOpen;
    lastUserOpen.current = threadsOpen;
    syncPane();
    // Read at adoption only.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isWide, mounted, panelRef, setThreadsOpen, shown, syncPane, threadsOpen]);

  // The pane's size changed (a drag, a toggle, a refused or retried expand): report what it shows.
  const onThreadsResize = useCallback(() => {
    const panel = panelRef.current;
    if (!panel || !adopted.current) return;
    reportRef.current?.(!panel.isCollapsed());
  }, [panelRef]);

  // Only a user's drag (or the separator's keys) collapsing or opening the pane changes their
  // choice. Layout changes made in code (a toggle, the auto-collapse, a refused expand) do not.
  const onLayoutChanged = useCallback(
    (layout: Layout, meta: LayoutChangedMeta) => {
      const panel = panelRef.current;
      if (!meta.isUserInteraction || !panel || !adopted.current) return;
      const open = !panel.isCollapsed();
      // A drag or a separator key sets the width an expand restores (the kit saves the layout
      // itself). From the reported layout: the DOM still has the previous one at this point.
      const percent = layout.threads;
      const element = groupElementRef.current;
      if (open && element && typeof percent === "number") {
        const width = paneWidthFromLayout(percent, element.getBoundingClientRect().width, separatorsWidth(element));
        if (width !== null && width > 0) {
          threadsWidth.current = width;
          writeThreadsWidth(agentId, width);
        }
      }
      if (open !== shownRef.current) setThreadsOpen(open);
    },
    [agentId, panelRef, setThreadsOpen],
  );

  if (!mounted) return <div data-testid="agent-chat-panes" className="flex min-h-0 min-w-0 flex-1" />;

  if (!isWide) {
    return (
      <div data-testid="agent-chat-panes" data-layout="narrow" className="flex min-h-0 min-w-0 flex-1 flex-col">
        {children}
        <Sheet open={threadsOpen} onOpenChange={setThreadsOpen}>
          <SheetContent size="md" aria-describedby={undefined} data-testid="agent-threads-sheet" className="left-0 right-auto border-l-0 border-r">
            <SheetHeader title={THREADS_TITLE} />
            <SheetBody className="flex flex-col p-0">{threads}</SheetBody>
          </SheetContent>
        </Sheet>
      </div>
    );
  }

  return (
    <ResizableGroup
      storageKey={storageKey}
      groupRef={groupRef}
      elementRef={setGroupElement}
      onLayoutChanged={onLayoutChanged}
      orientation="horizontal"
      data-testid="agent-chat-panes"
      data-layout="wide"
      className="flex min-h-0 flex-1"
    >
      <ResizablePanel
        id="threads"
        panelRef={panelRef}
        defaultSize={THREADS_DEFAULT_PX}
        minSize={THREADS_MIN_PX}
        collapsible
        onResize={onThreadsResize}
        data-testid="agent-threads-pane"
        className="min-w-0"
      >
        {/* A collapsed pane keeps its content mounted (the list keeps its state) but out of reach. */}
        <div className="flex h-full min-h-0 min-w-0 flex-col" hidden={!shown} aria-label={THREADS_TITLE} role="region">
          {threads}
        </div>
      </ResizablePanel>
      <ResizableHandle label="Resize conversations panel" />
      <ResizablePanel id="chat" minSize={CHAT_MIN_PX} className="min-w-0">
        {children}
      </ResizablePanel>
    </ResizableGroup>
  );
}
