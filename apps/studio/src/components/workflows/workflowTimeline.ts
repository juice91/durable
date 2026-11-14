/**
 * The run view's Timeline dock rows (SL.14), derived from `deriveRunView` (pure). One row per step
 * of the graph: steps that started, in the order they started (ties in graph order), then the ones
 * that have not, in graph order. Each row carries the step's state and, once both ends are known,
 * its duration and its offset within the run, for the bar.
 *
 * A step with no end of its own is closed at the timeline's clock (`timelineClock`): the run's end
 * once the run is terminal (a run that ended interrupts whatever was still running or waiting), and
 * the live "now" only while the run is still going.
 */
import { isTerminal, type NodeState, type WorkflowRunView } from "@/lib/workflowRunState";
import { buildWorkflowGraph } from "@/lib/workflowGraph";
import type { StepType, WorkflowSpec } from "@/lib/workflows";

export interface TimelineRow {
  stepId: string;
  stepType: StepType | null;
  state: NodeState;
  startedAt: string | null;
  endedAt: string | null;
  /** End minus start in ms (an interrupted step ends with the run); null while it is still open in a live run. */
  durationMs: number | null;
  /** Where the bar starts and how wide it is, as fractions of the run's span [0, 1]; null when the step has not started. */
  bar: { offset: number; width: number } | null;
}

function ms(iso: string | null): number | null {
  if (!iso) return null;
  const value = new Date(iso).getTime();
  return Number.isFinite(value) ? value : null;
}

/**
 * The instant that closes steps with no end of their own. A terminal run: its terminal event's ts,
 * else the run row's `updated_at`, else the latest step timestamp (never the wall clock, so an ended
 * run's bars do not move). A live run: `now`.
 */
export function timelineClock(view: WorkflowRunView, runUpdatedAt: string | null | undefined, now: number): number {
  if (!isTerminal(view.status)) return now;
  const end = ms(view.terminal?.ts ?? null) ?? ms(runUpdatedAt ?? null);
  if (end !== null) return end;
  const known = Object.values(view.steps).flatMap((d) => [ms(d.startedAt), ms(d.endedAt)]).filter((n): n is number => n !== null);
  return known.length > 0 ? Math.max(...known) : now;
}

/**
 * The dock rows for `view` over `spec`'s steps. `clock` (from `timelineClock`) closes the bar of a
 * step with no end; in a terminal run it also gives that interrupted step its duration.
 */
export function timelineRows(spec: WorkflowSpec | null, view: WorkflowRunView, clock: number): TimelineRow[] {
  if (!spec) return [];
  const ended = isTerminal(view.status);
  const order = buildWorkflowGraph(spec).nodes.map((node) => ({ id: node.id, type: node.step.type }));
  const rank = new Map(order.map((node, index) => [node.id, index]));
  const rows = order.map(({ id, type }): TimelineRow => {
    const detail = view.steps[id];
    const start = ms(detail?.startedAt ?? null);
    const end = ms(detail?.endedAt ?? null);
    return {
      stepId: id,
      stepType: detail?.stepType ?? type,
      state: detail?.state ?? "pending",
      startedAt: detail?.startedAt ?? null,
      endedAt: detail?.endedAt ?? null,
      durationMs: start !== null && end !== null && end >= start ? end - start : start !== null && ended ? Math.max(0, clock - start) : null,
      bar: null,
    };
  });

  const starts = rows.map((row) => ms(row.startedAt)).filter((n): n is number => n !== null);
  if (starts.length > 0) {
    const first = Math.min(...starts);
    const ends = rows.map((row) => ms(row.endedAt) ?? (ms(row.startedAt) !== null ? clock : null)).filter((n): n is number => n !== null);
    const last = Math.max(first, ...ends);
    const span = last - first;
    for (const row of rows) {
      const start = ms(row.startedAt);
      if (start === null) continue;
      const end = ms(row.endedAt) ?? Math.max(start, clock);
      const offset = span > 0 ? (start - first) / span : 0;
      const width = span > 0 ? Math.max(0, Math.min(1 - offset, (end - start) / span)) : 1;
      row.bar = { offset, width };
    }
  }

  return rows.sort((a, b) => {
    const sa = ms(a.startedAt);
    const sb = ms(b.startedAt);
    if (sa !== null && sb !== null && sa !== sb) return sa - sb;
    if ((sa === null) !== (sb === null)) return sa === null ? 1 : -1;
    return (rank.get(a.stepId) ?? 0) - (rank.get(b.stepId) ?? 0);
  });
}

/** "n of m steps done" for the collapsed dock bar: completed or skipped count as done. */
export function timelineSummary(rows: readonly TimelineRow[]): { done: number; total: number } {
  return { done: rows.filter((row) => row.state === "completed" || row.state === "skipped").length, total: rows.length };
}
