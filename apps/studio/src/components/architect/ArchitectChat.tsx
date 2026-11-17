"use client";

import React from "react";

export interface ArchitectChatProps {
  /** The Architect's agent row id (from GET /architect), or null before it is seeded. */
  architectAgentId: string | null;
  /** Call after a chat approval created or changed something, so the created list refreshes. */
  onChanged: () => void;
}

/** Placeholder for the Chat tab; Task 7 mounts the Architect conversation here. */
export function ArchitectChat(_props: ArchitectChatProps) {
  return <p data-testid="architect-chat-stub" className="text-body text-muted-foreground">Chat with the Architect is coming here.</p>;
}
