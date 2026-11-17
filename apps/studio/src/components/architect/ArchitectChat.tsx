"use client";

import React, { useEffect, useRef } from "react";
import { ChatTab } from "@/components/agentPage/ChatTab";
import { onToolDecided } from "@/lib/approvals";

export interface ArchitectChatProps {
  /** The Architect's agent row id (from GET /architect), or null before it is seeded. */
  architectAgentId: string | null;
  /** Call after a chat approval created or changed something, so the created list refreshes. */
  onChanged: () => void;
}

/**
 * The Chat tab: the Architect's own conversation, the same chat as an agent's page (threads pane,
 * composer, approval cards for its gated architect.* tools). The address stays `/architect`. After
 * any approval decision the created list refreshes, whether the call was approved (it ran) or denied.
 */
export function ArchitectChat({ architectAgentId, onChanged }: ArchitectChatProps) {
  const changed = useRef(onChanged);
  changed.current = onChanged;
  useEffect(() => onToolDecided(() => changed.current()), []);
  if (!architectAgentId) {
    return (
      <p data-testid="architect-chat-unavailable" className="text-body text-muted-foreground">
        The Architect is not set up yet, so there is nothing to chat with.
      </p>
    );
  }
  return (
    <div data-testid="architect-chat" className="flex h-[calc(100vh-16rem)] min-h-96 min-w-0 flex-col overflow-hidden rounded-md border border-border">
      <ChatTab agentId={architectAgentId} syncUrl={false} />
    </div>
  );
}
