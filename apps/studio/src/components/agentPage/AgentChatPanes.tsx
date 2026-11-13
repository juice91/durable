"use client";

import React, { useCallback, useEffect, useRef, useState } from "react";
import { useGroupRef, usePanelRef } from "react-resizable-panels";
import { ResizableGroup, ResizableHandle, ResizablePanel, readStoredLayout, writeStoredLayout } from "@/components/ui/Resizable";
import { Sheet, SheetBody, SheetContent, SheetHeader } from "@/components/ui/Sheet";
import { useIsWide } from "@/components/frame/useMediaQuery";
import { agentLayoutKey } from "./agentPageModel";
import { useAgentPage } from "./AgentPageContext";
import { CHAT_MIN_PX, THREADS_DEFAULT_PX, THREADS_MIN_PX, threadsShown } from "./chatFit";

// The pane minimums live in chatFit.ts (one source for the panes and the fit decisions).
export { CHAT_MIN_PX, THREADS_DEFAULT_PX, THREADS_MIN_PX };

export const THREADS_TITLE = "Conversations";

function clearStoredLayout(storageKey: string): void {
  try {
    window.localStorage.removeItem(storageKey);
  } catch {
    // Storage unavailable: nothing was saved either.
  }
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
  const storageKey = agentLayoutKey(agentId);

  // The first time the panes exist, the saved layout decides whether threads start collapsed; after
  // that, the toggle (and the auto-collapse) drives the pane.
  const adopted = useRef(false);
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
      lastUserOpen.current = savedOpen;
      if (savedOpen !== threadsOpen) {
        setThreadsOpen(savedOpen);
        return;
      }
    }
    const userChanged = lastUserOpen.current !== threadsOpen;
    lastUserOpen.current = threadsOpen;
    if (shown !== !panel.isCollapsed()) {
      if (shown) panel.expand();
      else panel.collapse();
    }
    // Only the user's choice is saved; an auto-collapse (or its undoing) is not.
    if (!userChanged) return;
    // The kit saves user drags; a toggle (`{`, the header button) changes the pane in code, so it is
    // saved here, and a reload keeps it.
    const layout = autoCollapsed ? undefined : groupRef.current?.getLayout();
    if (layout && Object.keys(layout).length > 0) writeStoredLayout(storageKey, layout);
    // No measured layout (the group has no size yet, or the pane is auto-collapsed): save the
    // collapse itself, or forget a saved one.
    else if (!threadsOpen) writeStoredLayout(storageKey, { threads: 0, chat: 100 });
    else if (readStoredLayout(storageKey)?.threads === 0) clearStoredLayout(storageKey);
  }, [autoCollapsed, groupRef, isWide, mounted, panelRef, setThreadsOpen, shown, storageKey, threadsOpen]);

  // Dragging the pane closed (or open) updates the toggle. A collapse or expand made in code (the
  // toggle, the auto-collapse) already matches `shown` and changes nothing here.
  // Read through a ref: the kit may report a resize with the callback from the render before.
  const shownRef = useRef(shown);
  shownRef.current = shown;
  const onThreadsResize = useCallback(() => {
    const panel = panelRef.current;
    if (!panel || !adopted.current) return;
    const open = !panel.isCollapsed();
    if (open !== shownRef.current) setThreadsOpen(open);
  }, [panelRef, setThreadsOpen]);

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
