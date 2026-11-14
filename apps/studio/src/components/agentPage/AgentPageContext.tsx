"use client";

import { createContext, useContext } from "react";
import type { Agent, AgentVersion } from "@/lib/api";
import type { VersionRef } from "@/components/chat/runOptions";

/**
 * What `app/agents/[id]/layout.tsx` (through `AgentLayout`) shares with its tabs: the agent, loaded
 * once, and the pane state the header buttons and the chat both use. Tabs rendered alone (in a test)
 * get null and fall back to their own loading and local state.
 */
export interface AgentPageValue {
  agentId: string;
  /** Null while loading or when the load failed. */
  agent: Agent | null;
  /** Published versions, newest first; null while loading. */
  versions: AgentVersion[] | null;
  loadError: string | null;
  /** Re-read the agent and its versions (after a save, publish or restore in the Editor tab). */
  reload: () => Promise<void>;
  /** The version the chat targets; the Config panel describes it. Null: the latest. */
  configVersion: VersionRef;
  setConfigVersion: (ref: VersionRef) => void;
  /**
   * Threads pane, the user's choice: expanded (wide screens) or its Sheet open (below 1024 px). What
   * the pane shows is `threadsOpen && !threadsAutoCollapsed`.
   */
  threadsOpen: boolean;
  /** A user's open or close (the toggle, `{`, a drag). Opening while auto-collapsed closes Config. */
  setThreadsOpen: (open: boolean) => void;
  /**
   * The same, for the user's explicit closes (the header toggle, `{`, the narrow Sheet's Escape or
   * outside tap): a close asks the registered close guard first (RM.18: an unsaved working memory
   * edit in the threads pane). A drag is not routed here: the wide pane stays mounted when collapsed.
   */
  requestThreadsOpen: (open: boolean) => void;
  /** Registers (or, with null, removes) the guard a close asks; it calls `close` to go ahead. */
  setThreadsCloseGuard: (guard: ThreadsCloseGuard | null) => void;
  /**
   * The threads pane is collapsed automatically because Config is open and threads + chat do not
   * fit beside it (chatFit.ts). Not the user's layout: never saved.
   */
  threadsAutoCollapsed: boolean;
  /**
   * Whether the wide threads pane really is expanded, as the panes last measured it; null while the
   * wide panes are not mounted. The header's pressed state follows it, so a pane that has not
   * expanded yet (an expand the layout refused) never reads as shown.
   */
  threadsPaneExpanded: boolean | null;
  /** The panes report their real state here (null when they unmount). */
  reportThreadsPane: (expanded: boolean | null) => void;
  /** The chat's current address (conversation and options), so the Chat tab returns to it. */
  chatHref: string | null;
  setChatHref: (href: string | null) => void;
}

/** Asked before the threads pane closes; calls `close` when the close may go ahead. */
export type ThreadsCloseGuard = (close: () => void) => void;

export const AgentPageContext = createContext<AgentPageValue | null>(null);

/** The agent page's shared state, or null outside `AgentLayout`. */
export function useAgentPage(): AgentPageValue | null {
  return useContext(AgentPageContext);
}
