"use client";

import React, { useId, useState } from "react";
import { ChevronLeft, ChevronRight } from "lucide-react";
import { cn } from "@/lib/cn";
import { ICON_STROKE_WIDTH } from "@/lib/icons";
import { Button } from "@/components/ui/Button";
import { IconButton } from "@/components/ui/IconButton";
import { Switch } from "@/components/ui/Switch";
import { adjacentIteration, iterationFailed, type IterationDetail, type LoopPager } from "@/lib/workflowRunState";
import { NODE_STATE_CLASSES, NODE_STATE_LABELS } from "./WorkflowGraph";
import { formatRunDuration } from "./WorkflowRunHistory";

/** Show `index` of the loop instance `key` (`LoopPager.key`; null: follow the latest iteration again). */
export type SelectIteration = (key: string, index: number | null) => void;

/** The pager's heading: "Iteration 3 of 7", or why there is none. */
export function pagerLabel(pager: LoopPager): string {
  if (!pager.selected) return "No iterations yet";
  return `Iteration ${pager.selected.index + 1} of ${pager.iterations.length}`;
}

/** A control at an end of the pager: looks disabled, keeps focus (aria-disabled, not disabled). */
const END_CLASS = "cursor-not-allowed opacity-50 hover:bg-transparent";

export interface IterationPagerProps {
  pager: LoopPager;
  /** Step through failed iterations only. */
  failedOnly: boolean;
  /** Keyed by the loop instance (`LoopPager.key`). */
  onFailedOnlyChange: (key: string, failedOnly: boolean) => void;
  onSelect: SelectIteration;
}

/**
 * One loop's iteration pager (loops D13): "Iteration 3 of 7", previous and next (←/→ also work in
 * the group), "Failed only", and "Latest" once the user has picked an iteration. The body's nodes,
 * the step list and the Timeline show the selected iteration.
 *
 * The controls never become `disabled` (that would drop focus to <body> when Previous reaches the
 * first iteration): at an end they are `aria-disabled` and do nothing. Only a move the user makes
 * is announced, never a new iteration of a live loop.
 */
export function IterationPager({ pager, failedOnly, onFailedOnlyChange, onSelect }: IterationPagerProps) {
  const switchId = useId();
  const current = pager.selected?.index ?? null;
  const failedCount = pager.iterations.filter(iterationFailed).length;
  const onlyFailed = failedOnly && failedCount > 0;
  const prev = adjacentIteration(pager.iterations, current, -1, onlyFailed);
  const next = adjacentIteration(pager.iterations, current, 1, onlyFailed);
  const [announcement, setAnnouncement] = useState("");
  const total = pager.iterations.length;
  const go = (index: number | null) => {
    if (index === null) return;
    onSelect(pager.key, index);
    setAnnouncement(`Iteration ${index + 1} of ${total}`);
  };
  const latest = () => {
    if (pager.following) return;
    onSelect(pager.key, null);
    const last = pager.iterations[total - 1];
    setAnnouncement(last ? `Latest: iteration ${last.index + 1} of ${total}` : "");
  };
  const toggleFailed = (checked: boolean) => {
    onFailedOnlyChange(pager.key, checked);
    // Turning it on while a passing iteration is shown jumps to the first failed one.
    if (checked && pager.selected && !iterationFailed(pager.selected)) go(adjacentIteration(pager.iterations, null, 1, true));
  };
  return (
    <div
      role="group"
      aria-label={`Iterations of ${pager.loopId}`}
      data-testid={`workflow-iteration-pager-${pager.loopId}`}
      data-depth={pager.depth}
      data-following={pager.following}
      className={cn("min-w-0 space-y-1.5 rounded-md border border-border bg-surface p-2", pager.depth > 0 && "ml-3")}
      onKeyDown={(event) => {
        if (event.target instanceof HTMLElement && event.target.getAttribute("role") === "switch") return;
        if (event.key === "ArrowLeft" && prev !== null) {
          event.preventDefault();
          go(prev);
        } else if (event.key === "ArrowRight" && next !== null) {
          event.preventDefault();
          go(next);
        }
      }}
    >
      <div className="flex min-w-0 flex-wrap items-center gap-x-2 gap-y-1">
        <span className="min-w-0 truncate font-mono text-label font-medium text-foreground">{pager.loopId}</span>
        {pager.selected ? (
          <span data-testid="workflow-iteration-pager-state" className={cn("rounded-sm border px-1 text-badge", NODE_STATE_CLASSES[pager.selected.state])}>
            {NODE_STATE_LABELS[pager.selected.state]}
          </span>
        ) : null}
      </div>
      <div className="flex min-w-0 flex-wrap items-center gap-1">
        <IconButton label="Previous iteration" size="sm" tooltip={false} aria-disabled={prev === null} className={cn(prev === null && END_CLASS)} onClick={() => go(prev)} icon={<ChevronLeft strokeWidth={ICON_STROKE_WIDTH} aria-hidden="true" />} />
        <span data-testid="workflow-iteration-pager-label" className="min-w-0 px-1 text-label tabular-nums text-foreground">{pagerLabel(pager)}</span>
        <IconButton label="Next iteration" size="sm" tooltip={false} aria-disabled={next === null} className={cn(next === null && END_CLASS)} onClick={() => go(next)} icon={<ChevronRight strokeWidth={ICON_STROKE_WIDTH} aria-hidden="true" />} />
        {total > 0 ? (
          <Button size="sm" variant="ghost" data-testid="workflow-iteration-pager-latest" aria-disabled={pager.following} className={cn(pager.following && END_CLASS)} onClick={latest}>Latest</Button>
        ) : null}
        <span role="status" data-testid="workflow-iteration-pager-announcement" className="sr-only">{announcement}</span>
      </div>
      <div className="flex min-w-0 items-center gap-2">
        <Switch id={switchId} checked={onlyFailed} disabled={failedCount === 0} onCheckedChange={toggleFailed} data-testid="workflow-iteration-pager-failed-only" />
        <label htmlFor={switchId} className={cn("text-caption", failedCount === 0 ? "text-muted-foreground" : "text-foreground")}>
          Failed only ({failedCount})
        </label>
      </div>
    </div>
  );
}

function iterationNote(it: IterationDetail): string {
  if (it.errorCode) return it.errorCode;
  if (it.until === "true") return "until met";
  if (it.until === "unknown") return "until unknown";
  return "";
}

export interface LoopIterationTableProps {
  pager: LoopPager;
  onSelect: SelectIteration;
}

/**
 * The loop detail's iteration list (loops D13): one row per iteration with its state, duration and
 * error code (or until verdict). A row shows that iteration in the graph. A long loop scrolls inside
 * the table rather than growing the panel.
 */
export function LoopIterationTable({ pager, onSelect }: LoopIterationTableProps) {
  if (pager.iterations.length === 0) {
    return <p data-testid="workflow-loop-iterations-empty" className="text-body text-muted-foreground">No iterations have started.</p>;
  }
  return (
    <div className="min-w-0">
      <h4 className="text-caption font-semibold uppercase tracking-wide text-muted-foreground">Iterations ({pager.iterations.length})</h4>
      <div className="mt-1 max-h-64 min-w-0 overflow-auto rounded-md border border-border">
        <table data-testid={`workflow-loop-iterations-${pager.loopId}`} className="w-full min-w-0 table-fixed border-collapse text-caption">
          <caption className="sr-only">Iterations of {pager.loopId}</caption>
          <thead className="sticky top-0 bg-surface-raised text-left text-muted-foreground">
            <tr>
              <th scope="col" className="w-12 px-2 py-1 font-medium">#</th>
              <th scope="col" className="w-28 px-2 py-1 font-medium">Status</th>
              <th scope="col" className="w-20 px-2 py-1 font-medium">Duration</th>
              <th scope="col" className="px-2 py-1 font-medium">Note</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-border">
            {pager.iterations.map((it) => {
              const shown = it === pager.selected;
              return (
                <tr key={it.scope} data-testid={`workflow-loop-iteration-${pager.loopId}-${it.index}`} data-state={it.state} data-shown={shown} className={cn(shown && "bg-fill-selected")}>
                  <td className="px-2 py-1">
                    <button
                      type="button"
                      aria-pressed={shown}
                      aria-label={`Show iteration ${it.index + 1}`}
                      onClick={() => onSelect(pager.key, it.index)}
                      className="rounded-sm font-mono tabular-nums text-info-text underline-offset-4 outline-none hover:underline focus-visible:ring-2 focus-visible:ring-ring"
                    >
                      {it.index + 1}
                    </button>
                  </td>
                  <td className="px-2 py-1">
                    <span className={cn("rounded-sm border px-1 text-badge", NODE_STATE_CLASSES[it.state])}>{NODE_STATE_LABELS[it.state]}</span>
                  </td>
                  <td className="whitespace-nowrap px-2 py-1 font-mono tabular-nums text-muted-foreground">
                    {it.startedAt && it.endedAt ? formatRunDuration(it.startedAt, it.endedAt) : it.state === "running" ? "in progress" : "—"}
                  </td>
                  <td className="truncate px-2 py-1 font-mono text-muted-foreground" title={it.error ?? undefined}>{iterationNote(it)}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}
