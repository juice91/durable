"use client";

import React, { useId } from "react";
import { ChevronDown, ChevronUp } from "lucide-react";
import { cn } from "@/lib/cn";
import { ICON_STROKE_WIDTH } from "@/lib/icons";
import type { NodeState } from "@/lib/workflowRunState";
import { NODE_STATE_CLASSES, NODE_STATE_LABELS, STEP_TYPE_LABELS } from "./WorkflowGraph";
import { DockResizeHandle } from "./WorkflowFloatingPanel";
import { formatDuration } from "./WorkflowRunHistory";
import { TIMELINE_BAR_HEIGHT } from "./workflowLayout";
import { timelineSummary, type TimelineRow } from "./workflowTimeline";

/** The bar colour of each state: the chart series tokens, critical for failures. */
const BAR_CLASSES: Record<NodeState, string> = {
  pending: "bg-transparent",
  running: "bg-[var(--viz-series-1-color)] animate-pulse motion-reduce:animate-none",
  waiting: "bg-warning",
  completed: "bg-[var(--viz-series-1-color)]",
  failed: "bg-[var(--viz-critical-color)]",
  skipped: "bg-muted-foreground/30",
  needs_attention: "bg-[var(--viz-critical-color)]",
};

export interface TimelineDockProps {
  rows: TimelineRow[];
  open: boolean;
  height: number;
  onOpenChange: (open: boolean) => void;
  onHeightChange: (height: number, persist: boolean) => void;
  selectedStepId: string | null;
  onSelectStep: (stepId: string) => void;
}

/**
 * The run view's bottom dock (decision "Workflows"): a 36 px bar with the step count, expanding to a
 * resizable list of per-step rows (state, duration and a bar placed within the run) taken from the
 * run's events. A row selects its step, like a node.
 */
export function TimelineDock({ rows, open, height, onOpenChange, onHeightChange, selectedStepId, onSelectStep }: TimelineDockProps) {
  const listId = useId();
  const { done, total } = timelineSummary(rows);
  return (
    <section
      aria-label="Timeline"
      data-testid="workflow-timeline-dock"
      data-open={open}
      // Expanded, the dock takes its saved height but never more than half the run view's canvas area.
      style={open ? { height, maxHeight: "50%" } : undefined}
      className="eap-viz flex min-h-0 shrink-0 flex-col border-t border-border bg-surface"
    >
      {open ? <DockResizeHandle height={height} onResize={onHeightChange} /> : null}
      <div style={{ height: TIMELINE_BAR_HEIGHT }} className="flex shrink-0 items-center gap-3 px-3">
        <button
          type="button"
          aria-expanded={open}
          aria-controls={open ? listId : undefined}
          data-testid="workflow-timeline-toggle"
          onClick={() => onOpenChange(!open)}
          className="inline-flex h-7 items-center gap-1.5 rounded-md px-2 text-label font-medium text-foreground outline-none hover:bg-fill-hover focus-visible:ring-2 focus-visible:ring-ring"
        >
          {open ? <ChevronDown className="size-icon" strokeWidth={ICON_STROKE_WIDTH} aria-hidden="true" /> : <ChevronUp className="size-icon" strokeWidth={ICON_STROKE_WIDTH} aria-hidden="true" />}
          Timeline
        </button>
        <span data-testid="workflow-timeline-summary" className="text-caption tabular-nums text-muted-foreground">
          {done} of {total} steps done
        </span>
      </div>
      {open ? (
        <div id={listId} className="min-h-0 flex-1 overflow-y-auto border-t border-border">
          {rows.length === 0 ? (
            <p className="p-3 text-label text-muted-foreground">No steps yet.</p>
          ) : (
            <ol aria-label="Step timeline" data-testid="workflow-timeline" className="divide-y divide-border">
              {rows.map((row) => (
                <li key={row.stepId} data-testid={`workflow-timeline-row-${row.stepId}`} data-state={row.state}>
                  <button
                    type="button"
                    aria-pressed={row.stepId === selectedStepId}
                    onClick={() => onSelectStep(row.stepId)}
                    className={cn(
                      "grid w-full min-w-0 grid-cols-[minmax(0,10rem)_minmax(0,1fr)_auto] items-center gap-3 px-3 py-1.5 text-left outline-none hover:bg-fill-hover focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring",
                      row.stepId === selectedStepId && "bg-fill-selected",
                    )}
                  >
                    <span className="flex min-w-0 flex-col">
                      <span className="truncate font-mono text-label text-foreground">{row.stepId}</span>
                      <span className="flex items-center gap-1.5 text-caption text-muted-foreground">
                        {row.stepType ? STEP_TYPE_LABELS[row.stepType] : null}
                        <span data-testid="workflow-timeline-state" className={cn("rounded-sm border px-1 text-badge", NODE_STATE_CLASSES[row.state])}>
                          {NODE_STATE_LABELS[row.state]}
                        </span>
                      </span>
                    </span>
                    <span aria-hidden="true" className="relative h-2 min-w-0 rounded-full bg-muted">
                      {row.bar ? (
                        <span
                          className={cn("absolute inset-y-0 min-w-1 rounded-full", BAR_CLASSES[row.state])}
                          style={{ left: `${row.bar.offset * 100}%`, width: `${row.bar.width * 100}%` }}
                        />
                      ) : null}
                    </span>
                    <span data-testid="workflow-timeline-duration" className="whitespace-nowrap font-mono text-caption tabular-nums text-muted-foreground">
                      {row.durationMs !== null ? formatDuration(row.startedAt, row.endedAt) : row.state === "running" || row.state === "waiting" ? "in progress" : "—"}
                    </span>
                  </button>
                </li>
              ))}
            </ol>
          )}
        </div>
      ) : null}
    </section>
  );
}
