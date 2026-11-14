"use client";

import React from "react";
import type { AgentSpec } from "@/lib/api";
import type { Conversation } from "@/lib/conversations";
import { ConversationRail } from "@/components/chat/ConversationRail";
import { MemoryCard, type MemoryCardInspector } from "./MemoryCard";

export interface ThreadsPanelProps {
  /** The agent; empty while it loads (nothing is listed). */
  agentId: string;
  selectedId: string | null;
  refreshKey: number;
  onSelect: (conversation: Conversation) => void;
  onNew: () => void;
  /** A conversation was deleted from its row menu (RM.18). */
  onDeleted?: (conversationId: string) => void;
  /** The effective spec the memory card summarises; null while unknown. */
  memorySpec: AgentSpec | null;
  /**
   * The memory inspector's inputs (RM.18); without them the card is the summary only. Its
   * `askDiscard` (the chat's, which keeps the working memory edit above the panes) is also asked
   * before opening another conversation or a new one.
   */
  memoryInspector?: MemoryCardInspector;
}

/**
 * The threads pane's content (decision "Agent chat panes", left): "New conversation", the recent
 * conversations with rename and delete, and the memory card pinned at the bottom, which expands into
 * the open conversation's memory inspector. While a working memory edit is unsaved, opening another
 * conversation (or a new one) asks first (the chat's Discard dialog).
 */
export function ThreadsPanel({ agentId, selectedId, refreshKey, onSelect, onNew, onDeleted, memorySpec, memoryInspector }: ThreadsPanelProps) {
  const discard = memoryInspector?.askDiscard;
  const ask = (proceed: () => void) =>
    discard ? discard(proceed, "The working memory edit is not saved. Opening another conversation discards it.") : proceed();
  return (
    <div data-testid="threads-panel" className="flex h-full min-h-0 min-w-0 flex-1 flex-col">
      <div className="flex min-h-0 flex-1 flex-col p-2">
        <ConversationRail
          agentId={agentId}
          selectedId={selectedId}
          refreshKey={refreshKey}
          onSelect={(conversation) => (conversation.id === selectedId ? onSelect(conversation) : ask(() => onSelect(conversation)))}
          onNew={() => ask(onNew)}
          onDeleted={onDeleted}
        />
      </div>
      <MemoryCard spec={memorySpec} inspector={memoryInspector} />
    </div>
  );
}
