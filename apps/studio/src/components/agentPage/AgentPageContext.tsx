"use client";

import { createContext, useContext } from "react";
import type { Agent, AgentVersion } from "@/lib/api";
import type { VersionRef } from "@/components/playground/runOptions";

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
   * The threads pane is collapsed automatically because Config is open and threads + chat do not
   * fit beside it (chatFit.ts). Not the user's layout: never saved.
   */
  threadsAutoCollapsed: boolean;
  /** Where the chat portals its temporary "Request context" section (inside the Config panel); null
   * while the Config panel is closed. */
  requestContextSlot: HTMLElement | null;
  /** The chat's current address (conversation and options), so the Chat tab returns to it. */
  chatHref: string | null;
  setChatHref: (href: string | null) => void;
}

export const AgentPageContext = createContext<AgentPageValue | null>(null);

/** The agent page's shared state, or null outside `AgentLayout`. */
export function useAgentPage(): AgentPageValue | null {
  return useContext(AgentPageContext);
}
