"use client";

import React from "react";
import type { AgentSpec } from "@/lib/api";
import { Badge } from "@/components/ui/Badge";
import { memorySummary } from "./agentPageModel";

/**
 * The memory card pinned under the conversation list: four on/off badges read from the effective
 * spec. Read-only until RM.18 makes it expand into the memory inspector.
 */
export function MemoryCard({ spec }: { spec: AgentSpec | null | undefined }) {
  const badges = memorySummary(spec);
  return (
    <section aria-labelledby="agent-memory-card-title" data-testid="memory-card" className="shrink-0 border-t border-border px-3 py-2">
      <h2 id="agent-memory-card-title" className="mb-1.5 text-caption font-semibold uppercase tracking-wide text-muted-foreground">
        Memory
      </h2>
      <ul className="flex flex-wrap gap-1">
        {badges.map((badge) => (
          <li key={badge.id}>
            <Badge
              variant={badge.on ? "success" : "neutral"}
              data-testid={`memory-badge-${badge.id}`}
              data-state={badge.on ? "on" : "off"}
            >
              {badge.label}
              <span className="sr-only">{badge.on ? ": on" : ": off"}</span>
              {badge.on ? null : <span aria-hidden="true"> off</span>}
            </Badge>
          </li>
        ))}
      </ul>
    </section>
  );
}
