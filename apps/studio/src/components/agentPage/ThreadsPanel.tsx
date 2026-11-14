"use client";

import React from "react";
import type { AgentSpec } from "@/lib/api";
import type { Conversation } from "@/lib/conversations";
import { ConversationRail } from "@/components/chat/ConversationRail";
import { MemoryCard } from "./MemoryCard";

export interface ThreadsPanelProps {
  /** The agent; empty while it loads (nothing is listed). */
  agentId: string;
  selectedId: string | null;
  refreshKey: number;
  onSelect: (conversation: Conversation) => void;
  onNew: () => void;
  /** The effective spec the memory card summarises; null while unknown. */
  memorySpec: AgentSpec | null;
}

/**
 * The threads pane's content (decision "Agent chat panes", left): "New conversation", the recent
 * conversations with rename, and the memory card pinned at the bottom.
 */
export function ThreadsPanel({ agentId, selectedId, refreshKey, onSelect, onNew, memorySpec }: ThreadsPanelProps) {
  return (
    <div data-testid="threads-panel" className="flex h-full min-h-0 min-w-0 flex-1 flex-col">
      <div className="flex min-h-0 flex-1 flex-col p-2">
        <ConversationRail agentId={agentId} selectedId={selectedId} refreshKey={refreshKey} onSelect={onSelect} onNew={onNew} />
      </div>
      <MemoryCard spec={memorySpec} />
    </div>
  );
}
