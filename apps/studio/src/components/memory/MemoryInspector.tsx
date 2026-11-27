"use client";

import { usePermissions } from "@/lib/permissions";
import React from "react";
import Link from "next/link";
import type { AgentSpec } from "@/lib/api";
import type { RecallSummary, WorkingMemoryTarget } from "@/lib/memory";
import { routes } from "@/lib/routes";
import { memoryOf } from "@/components/agents/knowledgeMemory";
import { ObservationsList } from "./ObservationsList";
import { WorkingMemoryEditor, type WorkingMemoryDraftStore } from "./WorkingMemoryEditor";
import { editBlockedReason, workingFormat, workingOf, workingScope, workingStarter } from "./memoryModel";
import type { MemoryActivity } from "./useMemoryActivity";

/** The "Recall (last turn)" line: counts only (events never carry the recalled text). */
export function recallText(recall: RecallSummary): string {
  if (recall.status === "none") return "Nothing recalled in the last turn (no earlier turns of this user were indexed yet, or none matched).";
  if (recall.status === "failed") return `Recall failed in the last turn${recall.errorCode ? ` (${recall.errorCode})` : ""}; the turn ran without it.`;
  if (recall.turns === 0) return "Recall ran in the last turn and found no related earlier turns.";
  const turns = `${recall.turns} earlier ${recall.turns === 1 ? "turn" : "turns"}`;
  const messages = `${recall.messages} ${recall.messages === 1 ? "message" : "messages"}`;
  return `The last turn recalled ${turns} (${recall.hits} ${recall.hits === 1 ? "hit" : "hits"}); ${messages} in ${recall.groups} ${recall.groups === 1 ? "group" : "groups"} reached the model.`;
}

/**
 * The working memory note the inspector shows for this conversation and spec (its scope decides), or
 * null with why there is none (no working memory, no conversation, no user). The chat uses the same
 * rule to know whether a lifted draft is the one on screen.
 */
export function inspectorTarget(
  agentId: string,
  conversationId: string | null,
  resourceId: string | null | undefined,
  spec: AgentSpec | null,
): { target: WorkingMemoryTarget | null; unavailable: string | null } {
  const working = workingOf(spec);
  if (!working || !conversationId) return { target: null, unavailable: null };
  if (workingScope(working) === "conversation") return { target: { scope: "conversation", conversationId }, unavailable: null };
  if (resourceId) return { target: { scope: "resource", agentId, resourceId }, unavailable: null };
  return {
    target: null,
    unavailable: resourceId === null ? "This conversation has no user id, so per-user working memory is off for it." : "Loading the conversation's user…",
  };
}

export interface MemoryInspectorProps {
  agentId: string;
  conversationId: string | null;
  /** The conversation's user: a resource id, null (none: per-user memory off), undefined (loading). */
  resourceId: string | null | undefined;
  /** The spec the chat runs (which features are on). */
  spec: AgentSpec | null;
  /** The latest published version's spec (what the API checks edits against). */
  publishedSpec: AgentSpec | null;
  activity: MemoryActivity;
  /** Where the working memory edit lives (the chat keeps it above the panes). */
  draftStore?: WorkingMemoryDraftStore;
}

/** What the inspector asks about. */
const NOTE_ACTIONS = ["run", "edit"] as const;

/**
 * The memory card's expanded view for the open conversation: working memory (view and edit), the
 * observations (delete newest or all), what recall brought into the last turn, and "Memory
 * updating" while a finished turn's post-turn steps have not reported.
 */
export function MemoryInspector({ agentId, conversationId, resourceId, spec, publishedSpec, activity, draftStore }: MemoryInspectorProps) {
  const memory = memoryOf(spec);
  // A conversation's notes are changed with `run` on its agent, a user's notes with `edit`.
  const permissions = usePermissions("agent", agentId, NOTE_ACTIONS);
  if (!memory) return null;
  if (!conversationId) {
    return (
      <p data-testid="memory-inspector-empty" className="text-label text-muted-foreground">
        Open or start a conversation to see its memory.
      </p>
    );
  }
  const working = workingOf(spec);
  const scope = workingScope(working);
  // Create's shape follows what the API validates against: the latest published working memory,
  // when there is one (as on the Memory tab), else the spec the chat runs.
  const shape = workingOf(publishedSpec) ?? working;
  const { target, unavailable } = inspectorTarget(agentId, conversationId, resourceId, spec);

  return (
    <div data-testid="memory-inspector" className="min-w-0 space-y-4">
      {/* One live region, always mounted, so the change is announced. */}
      <p role="status" className={activity.updating ? "text-caption text-info-text" : "sr-only"}>
        {activity.updating ? <span data-testid="memory-updating">Memory updating…</span> : null}
      </p>
      {working ? (
        <WorkingMemoryEditor
          target={target}
          unavailable={unavailable}
          mayChange={permissions.can(scope === "resource" ? "edit" : "run")}
          format={workingFormat(shape)}
          starter={workingStarter(shape)}
          editBlocked={editBlockedReason(publishedSpec, scope)}
          refreshKey={activity.revision}
          draftStore={draftStore}
          compact
          hint={
            scope === "resource" && resourceId ? (
              <>
                Per user: shared by every conversation of <span className="font-mono">{resourceId}</span>.{" "}
                <Link href={routes.agentMemory(agentId, { resource: resourceId })} className="text-info-text underline underline-offset-2">
                  Open in Memory
                </Link>
              </>
            ) : scope === "conversation" ? (
              "Per conversation."
            ) : null
          }
        />
      ) : null}
      {memory.observational ? <ObservationsList conversationId={conversationId} refreshKey={activity.revision} mayDelete={permissions.can("run")} /> : null}
      {memory.semantic_recall ? (
        <section data-testid="memory-recall" aria-label="Recall in the last turn" className="min-w-0 space-y-1">
          <h3 className="text-label font-semibold text-foreground">Recall (last turn)</h3>
          <p data-testid="memory-recall-summary" data-status={activity.recall.status} className="text-caption text-muted-foreground">
            {recallText(activity.recall)}
          </p>
        </section>
      ) : null}
    </div>
  );
}
