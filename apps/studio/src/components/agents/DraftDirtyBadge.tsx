"use client";

import React from "react";
import { Badge } from "@/components/ui/Badge";
import { Tooltip } from "@/components/ui/Tooltip";

export const DRAFT_DIRTY_LABEL = "草稿未發佈";

/**
 * 「草稿未發佈」: the draft differs from the latest published version (the API's `draft_dirty`).
 * Shared by the agents list and the agent page header. Renders nothing unless `dirty`.
 */
export function DraftDirtyBadge({ dirty, version }: { dirty?: boolean; version: number | null | undefined }) {
  if (!dirty) return null;
  const tip = `The draft differs from v${version ?? "?"}; open the editor to publish or discard.`;
  return (
    <Tooltip content={tip}>
      <span data-testid="draft-dirty-badge" tabIndex={0} aria-label={`${DRAFT_DIRTY_LABEL}. ${tip}`}>
        <Badge variant="warning">{DRAFT_DIRTY_LABEL}</Badge>
      </span>
    </Tooltip>
  );
}
