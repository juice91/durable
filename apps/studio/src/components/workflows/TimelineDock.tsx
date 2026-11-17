"use client";

import React, { useId } from "react";
import { ChevronDown, ChevronUp, Download } from "lucide-react";
import { IconButton } from "@/components/ui/IconButton";
import { downloadJson } from "@/lib/download";
import { cn } from "@/lib/cn";
import { ICON_STROKE_WIDTH } from "@/lib/icons";
import type { NodeState } from "@/lib/workflowRunState";
import { NODE_STATE_CLASSES, NODE_STATE_LABELS, stepTypeLabel } from "./WorkflowGraph";
import { DockResizeHandle } from "./WorkflowFloatingPanel";
import { formatRunDuration } from "./WorkflowRunHistory";
import { TIMELINE_BAR_HEIGHT } from "./workflowLayout";
import { timelineSummary, type TimelineRow } from "./workflowTimeline";

/** The bar colour of each state: the chart series tokens, critical for failures. */
/** Left padding per loop depth (0: top level; deeper levels as deep as the step list's nesting). */
const TIMELINE_INDENT = ["", "pl-7", "pl-11", "pl-14", "pl-16"] as const;

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
  /** The run's id, in the names of the downloaded files. */
  runId: string;
  rows: TimelineRow[];
  open: boolean;
  height: number;
  onOpenChange: (open: boolean) => void;
  onHeightChange: (height: number, persist: boolean) => void;
  selectedStepId: string | null;
  onSelectStep: (stepId: string) => void;
  /** An iteration row was chosen: show that iteration (the pager's choice by loop instance key, LP.12). */
  onSelectIteration?: (pagerKey: string, index: number) => void;
}

/**
 * The run view's bottom dock (decision "Workflows"): a 36 px bar with the step count, expanding to a
 * resizable list of per-step rows (state, duration and a bar placed within the run) taken from the
 * run's events. A row selects its step, like a node.
 */
export function TimelineDock({ runId, rows, open, height, onOpenChange, onHeightChange, selectedStepId, onSelectStep, onSelectIteration }: TimelineDockProps) {
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
              {rows.map((row) => {
                const it = row.iteration;
                const pressed = it ? it.shown : row.stepId === selectedStepId;
                return (
                <li
                  key={row.key}
                  data-testid={it ? `workflow-timeline-iteration-${it.loopId}-${it.index}` : `workflow-timeline-row-${row.stepId}`}
                  data-kind={row.kind}
                  data-state={row.state}
                  data-depth={row.depth}
                  {...(it ? { "data-shown": String(it.shown) } : {})}
                  className="flex items-center"
                >
                  <button
                    type="button"
                    aria-pressed={pressed}
                    aria-label={it ? `${row.stepId} iteration ${it.index + 1}, ${NODE_STATE_LABELS[row.state]}${it.shown ? ", shown" : ""}` : undefined}
                    onClick={() => (it ? onSelectIteration?.(it.pagerKey, it.index) : onSelectStep(row.stepId))}
                    className={cn(
                      "grid min-w-0 flex-1 grid-cols-[minmax(0,10rem)_minmax(0,1fr)_auto] items-center gap-3 px-3 py-1.5 text-left outline-none hover:bg-fill-hover focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring",
                      // A loop's iterations and body rows are indented under it, as in the step list.
                      TIMELINE_INDENT[Math.min(row.depth, TIMELINE_INDENT.length - 1)],
                      pressed && "bg-fill-selected",
                    )}
                  >
                    <span className="flex min-w-0 flex-col">
                      {it ? (
                        <span className="truncate text-label text-foreground">Iteration <span className="font-mono tabular-nums">{it.index + 1}</span></span>
                      ) : (
                        <span className="truncate font-mono text-label text-foreground">{row.stepId}</span>
                      )}
                      <span className="flex min-w-0 items-center gap-1.5 text-caption text-muted-foreground">
                        {row.stepType ? stepTypeLabel(row.stepType) : null}
                        <span data-testid="workflow-timeline-state" className={cn("rounded-sm border px-1 text-badge", NODE_STATE_CLASSES[row.state])}>
                          {NODE_STATE_LABELS[row.state]}
                        </span>
                        {it?.errorCode ? <code className="truncate font-mono text-badge">{it.errorCode}</code> : null}
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
                      {row.durationMs !== null ? formatRunDuration(row.startedAt, row.endedAt) : row.state === "running" || row.state === "waiting" ? "in progress" : "—"}
                    </span>
                  </button>
                  {it ? null : (
                    <span className="flex shrink-0 items-center gap-0.5 pr-2">
                      <IconButton
                        label="Download input"
                        size="sm"
                        tooltip={false}
                        icon={<Download strokeWidth={ICON_STROKE_WIDTH} aria-hidden="true" />}
                        disabled={row.input === undefined}
                        onClick={() => downloadJson(`${runId}-${row.stepId}-input.json`, row.input)}
                      />
                      <IconButton
                        label="Download output"
                        size="sm"
                        tooltip={false}
                        icon={<Download strokeWidth={ICON_STROKE_WIDTH} aria-hidden="true" />}
                        disabled={!row.hasOutput}
                        onClick={() => downloadJson(`${runId}-${row.stepId}-output.json`, row.output)}
                      />
                    </span>
                  )}
                </li>
                );
              })}
            </ol>
          )}
        </div>
      ) : null}
    </section>
  );
}
