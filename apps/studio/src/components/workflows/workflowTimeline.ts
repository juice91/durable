/**
 * The run view's Timeline dock rows (SL.14), derived from `deriveRunView` (pure). One row per step
 * of the graph, plus (LP.12) one per loop iteration, grouping the shown iteration's body rows: steps
 * that started, in the order they started (ties in graph order), then the ones that have not, in
 * graph order. Each row carries the step's state and, once both ends are known,
 * its duration and its offset within the run, for the bar.
 *
 * A step with no end of its own is closed at the timeline's clock (`timelineClock`): the run's end
 * once the run is terminal (a run that ended interrupts whatever was still running or waiting), and
 * the live "now" only while the run is still going.
 */
import { instanceKeyOfIteration, isTerminal, resolveRunStates, type IterationDetail, type NodeState, type ResolvedRunStates, type WorkflowRunView } from "@/lib/workflowRunState";
import { buildWorkflowGraph } from "@/lib/workflowGraph";
import type { StepType, WorkflowSpec } from "@/lib/workflows";

export interface TimelineRow {
  /** Unique per row: the step id, or `iteration:{scope}` for an iteration group row. */
  key: string;
  /** A step, or one iteration of a loop (LP.12: the loop's iterations group its body rows). */
  kind: "step" | "iteration";
  /** The step id; for an iteration row, its loop's id. */
  stepId: string;
  /** Iteration rows only: which iteration, and whether it is the one the body rows below show. */
  iteration?: { loopId: string; index: number; scope: string; shown: boolean; errorCode: string | null; pagerKey: string };
  stepType: StepType | null;
  state: NodeState;
  startedAt: string | null;
  endedAt: string | null;
  /** End minus start in ms (an interrupted step ends with the run); null while it is still open in a live run. */
  durationMs: number | null;
  /** Where the bar starts and how wide it is, as fractions of the run's span [0, 1]; null when the step has not started. */
  bar: { offset: number; width: number } | null;
  /** The enclosing loop step's id (a loop body step or an iteration row), or null at the top level. */
  parentId: string | null;
  /** The indent level: 0 at the top level; a loop's iteration rows and body rows go one deeper each. */
  depth: number;
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
  const known = [
    ...Object.values(view.steps).flatMap((d) => [d.startedAt, d.endedAt]),
    ...Object.values(view.iterations ?? {}).flatMap((it) => [it.startedAt, it.endedAt]),
  ].map(ms).filter((n): n is number => n !== null);
  return known.length > 0 ? Math.max(...known) : now;
}

/**
 * The dock rows for `view` over `spec`'s steps. `clock` (from `timelineClock`) closes the bar of a
 * step with no end; in a terminal run it also gives that interrupted step its duration. `resolved`
 * (from `resolveRunStates`, the pager's choice) says which iteration each loop shows: a loop's row is
 * followed by one row per iteration, and its body rows follow the shown iteration's row only, so a
 * long loop never flattens into iterations × body rows. A loop with no iteration yet keeps its body
 * rows directly under it.
 */
export function timelineRows(spec: WorkflowSpec | null, view: WorkflowRunView, clock: number, resolved?: ResolvedRunStates): TimelineRow[] {
  if (!spec) return [];
  const shown = resolved ?? resolveRunStates(spec, view);
  const ended = isTerminal(view.status);
  const nodes = buildWorkflowGraph(spec).nodes;
  const rank = new Map(nodes.map((node, index) => [node.id, index]));
  const duration = (startedAt: string | null, endedAt: string | null) => {
    const start = ms(startedAt);
    const end = ms(endedAt);
    return start !== null && end !== null && end >= start ? end - start : start !== null && ended ? Math.max(0, clock - start) : null;
  };
  const stepRow = (id: string, type: StepType, parentId: string | null): TimelineRow => {
    const detail = shown.details[id] ?? view.steps[id];
    return {
      key: id,
      kind: "step",
      stepId: id,
      stepType: detail?.stepType ?? type,
      state: detail?.state ?? "pending",
      startedAt: detail?.startedAt ?? null,
      endedAt: detail?.endedAt ?? null,
      durationMs: duration(detail?.startedAt ?? null, detail?.endedAt ?? null),
      bar: null,
      parentId,
      depth: 0,
    };
  };
  const iterationRow = (it: IterationDetail, selected: IterationDetail | null): TimelineRow => ({
    key: `iteration:${it.scope}`,
    kind: "iteration",
    stepId: it.loopId,
    iteration: { loopId: it.loopId, index: it.index, scope: it.scope, shown: it === selected, errorCode: it.errorCode, pagerKey: instanceKeyOfIteration(it.scope) },
    stepType: null,
    state: it.state,
    startedAt: it.startedAt,
    endedAt: it.endedAt,
    durationMs: duration(it.startedAt, it.endedAt),
    bar: null,
    parentId: it.loopId,
    depth: 0,
  });

  const compare = (a: TimelineRow, b: TimelineRow) => {
    const sa = ms(a.startedAt);
    const sb = ms(b.startedAt);
    if (sa !== null && sb !== null && sa !== sb) return sa - sb;
    if ((sa === null) !== (sb === null)) return sa === null ? 1 : -1;
    return (rank.get(a.stepId) ?? 0) - (rank.get(b.stepId) ?? 0);
  };
  const byParent = new Map<string | null, TimelineRow[]>();
  for (const node of nodes) {
    if (!byParent.has(node.parentId)) byParent.set(node.parentId, []);
    byParent.get(node.parentId)!.push(stepRow(node.id, node.step.type, node.parentId));
  }
  const pagers = new Map(shown.pagers.map((pager) => [pager.loopId, pager]));
  // Each level is ordered on its own; a loop's iteration rows and body rows follow the loop's row
  // (as in the step list), so a loop-free spec keeps exactly the flat order.
  const out: TimelineRow[] = [];
  const emit = (parentId: string | null, depth: number) => {
    for (const row of (byParent.get(parentId) ?? []).sort(compare)) {
      row.depth = depth;
      out.push(row);
      const pager = pagers.get(row.stepId);
      if (!pager || pager.iterations.length === 0) {
        emit(row.stepId, depth + 1);
        continue;
      }
      for (const it of pager.iterations) {
        const itRow = iterationRow(it, pager.selected);
        itRow.depth = depth + 1;
        out.push(itRow);
        if (it === pager.selected) emit(row.stepId, depth + 2);
      }
    }
  };
  emit(null, 0);

  const starts = out.map((row) => ms(row.startedAt)).filter((n): n is number => n !== null);
  if (starts.length > 0) {
    const first = Math.min(...starts);
    const ends = out.map((row) => ms(row.endedAt) ?? (ms(row.startedAt) !== null ? clock : null)).filter((n): n is number => n !== null);
    const last = Math.max(first, ...ends);
    const span = last - first;
    for (const row of out) {
      const start = ms(row.startedAt);
      if (start === null) continue;
      const end = ms(row.endedAt) ?? Math.max(start, clock);
      const offset = span > 0 ? (start - first) / span : 0;
      const width = span > 0 ? Math.max(0, Math.min(1 - offset, (end - start) / span)) : 1;
      row.bar = { offset, width };
    }
  }
  return out;
}

/** "n of m steps done" for the collapsed dock bar: completed or skipped count as done. */
export function timelineSummary(rows: readonly TimelineRow[]): { done: number; total: number } {
  // Iteration rows group steps; they are not steps themselves.
  const steps = rows.filter((row) => row.kind !== "iteration");
  return { done: steps.filter((row) => row.state === "completed" || row.state === "skipped").length, total: steps.length };
}
