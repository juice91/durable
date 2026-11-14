"use client";

import React, { useEffect, useId, useState } from "react";
import { ChevronDown } from "lucide-react";
import type { AgentSpec } from "@/lib/api";
import { cn } from "@/lib/cn";
import { ICON_STROKE_WIDTH } from "@/lib/icons";
import { Badge } from "@/components/ui/Badge";
import { focusRing } from "@/components/ui/Button";
import { memoryOf } from "@/components/agents/knowledgeMemory";
import { MemoryInspector } from "@/components/memory/MemoryInspector";
import { readMemoryCardOpen, writeMemoryCardOpen } from "@/components/memory/memoryModel";
import type { MemoryActivity } from "@/components/memory/useMemoryActivity";
import type { WorkingMemoryDraftStore } from "@/components/memory/WorkingMemoryEditor";
import { memorySummary } from "./agentPageModel";

/** What the card needs to expand into the inspector (RM.18); without it the card is the summary only. */
export interface MemoryCardInspector {
  agentId: string;
  conversationId: string | null;
  /** The open conversation's user (resource id); null: none; undefined: not known yet. */
  resourceId: string | null | undefined;
  /** The latest published version's spec (what the API checks working memory edits against). */
  publishedSpec: AgentSpec | null;
  activity: MemoryActivity;
  /** The working memory edit, kept above the panes so hiding the card or a remount keeps it. */
  draftStore?: WorkingMemoryDraftStore;
  /** Runs `proceed` at once, or after "Discard edit" while the edit is unsaved. */
  askDiscard?: (proceed: () => void, reason?: string) => void;
}

const IDLE_ACTIVITY: MemoryActivity = { recall: { status: "none" }, revision: 0, updating: false };

/**
 * The memory card pinned under the conversation list: four on/off badges read from the effective
 * spec, through the same helper as the editor's Memory step (`memoryFeatures` in
 * components/agents/knowledgeMemory.ts). For an agent with memory it expands (the chevron, state
 * kept in `localStorage["studio.memory-card.v1"]`) into the open conversation's memory inspector
 * (RM.18): working memory, observations and the last turn's recall.
 */
export function MemoryCard({ spec, inspector }: { spec: AgentSpec | null | undefined; inspector?: MemoryCardInspector }) {
  const badges = memorySummary(spec);
  const hasMemory = memoryOf(spec) !== null;
  const expandable = Boolean(inspector) && hasMemory;
  const [open, setOpen] = useState(false);
  const panelId = useId();
  // Read after mount: the server render cannot know the stored choice.
  useEffect(() => setOpen(readMemoryCardOpen()), []);
  const expanded = expandable && open;
  const toggle = () => {
    const next = !open;
    const apply = () => {
      setOpen(next);
      writeMemoryCardOpen(next);
    };
    // Hiding asks first while the working memory edit is unsaved (the edit is discarded).
    if (!next && inspector?.askDiscard) inspector.askDiscard(apply, "The working memory edit is not saved. Hiding the memory inspector discards it.");
    else apply();
  };
  const activity = inspector?.activity ?? IDLE_ACTIVITY;

  return (
    <section
      aria-labelledby="agent-memory-card-title"
      data-testid="memory-card"
      data-expanded={expanded ? "true" : "false"}
      className={cn("flex shrink-0 flex-col border-t border-border px-3 py-2", expanded && "max-h-[60%] min-h-0")}
    >
      <div className="mb-1.5 flex min-w-0 items-center gap-2">
        <h2 id="agent-memory-card-title" className="text-caption font-semibold uppercase tracking-wide text-muted-foreground">
          Memory
          {/* "Memory status": the Config panel's Memory section is another region on the same page. */}
          <span className="sr-only"> status</span>
        </h2>
        {activity.updating && !expanded ? (
          <span data-testid="memory-card-updating" className="text-caption text-info-text">
            Updating…
          </span>
        ) : null}
        {expandable ? (
          <button
            type="button"
            aria-expanded={expanded}
            aria-controls={expanded ? panelId : undefined}
            onClick={toggle}
            data-testid="memory-card-toggle"
            className={cn("ml-auto inline-flex h-6 items-center gap-1 rounded-sm px-1.5 text-caption text-muted-foreground hover:bg-fill-hover hover:text-foreground", focusRing)}
          >
            {expanded ? "Hide" : "Inspect"}
            <span className="sr-only"> memory</span>
            <ChevronDown aria-hidden="true" strokeWidth={ICON_STROKE_WIDTH} className={cn("size-3.5 transition-transform", expanded ? "rotate-180" : "")} />
          </button>
        ) : null}
      </div>
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
      {expanded && inspector ? (
        <div id={panelId} className="mt-3 min-h-0 overflow-y-auto pb-1">
          <MemoryInspector
            agentId={inspector.agentId}
            conversationId={inspector.conversationId}
            resourceId={inspector.resourceId}
            spec={spec ?? null}
            publishedSpec={inspector.publishedSpec}
            activity={activity}
            draftStore={inspector.draftStore}
          />
        </div>
      ) : null}
    </section>
  );
}
