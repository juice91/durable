// A workflow spec as a graph: one node per step, one edge per hand-off, and a deterministic layered
// layout (layer = longest path from `start`, order within a layer = first visit in a breadth-first
// walk that follows each step's targets in declaration order). Pure; no React Flow types here.
import { isLoopStep, type Expression, type LoopKindStep, type WorkflowSpec, type WorkflowStep } from "./workflows";

export type EdgeKind = "next" | "case" | "default" | "fork" | "join";

export interface GraphNode {
  id: string;
  step: WorkflowStep;
  layer: number;
  order: number;
  /** Relative to the enclosing group when `parentId` is set, else absolute. */
  x: number;
  y: number;
  /** The enclosing loop step's id (a body step), or null at the top level. */
  parentId: string | null;
  /** 0 at the top level, 1 in a loop body, 2 in a nested loop's body. */
  depth: number;
  width: number;
  height: number;
  /** "group": an expanded loop holding its body. A collapsed loop is a "step" with `collapsed`. */
  kind: "step" | "group";
  collapsed: boolean;
}

export interface GraphEdge {
  id: string;
  source: string;
  target: string;
  kind: EdgeKind;
  /** Branch cases: "1: input.urgent eq true"; default: "default"; join: "join". */
  label?: string;
}

export interface WorkflowGraph {
  nodes: GraphNode[];
  edges: GraphEdge[];
}

export const NODE_WIDTH = 200;
export const NODE_HEIGHT = 64;
const H_GAP = 60;
const V_GAP = 70;
/** A loop group: its header (id, kind, badge and two summary lines) and padding round its body. */
export const GROUP_HEADER_HEIGHT = 76;
export const GROUP_PAD_X = 16;
export const GROUP_PAD_BOTTOM = 16;
export const GROUP_MIN_WIDTH = NODE_WIDTH + 2 * GROUP_PAD_X;
const MAX_OPERAND = 32;

function truncate(text: string, max = MAX_OPERAND): string {
  return text.length > max ? `${text.slice(0, max - 1)}…` : text;
}

/** A $ref shows as its path, anything else as compact JSON. */
export function operandSummary(value: unknown): string {
  if (value && typeof value === "object" && !Array.isArray(value)) {
    const ref = (value as Record<string, unknown>)["$ref"];
    if (typeof ref === "string" && Object.keys(value).length === 1) return truncate(ref);
  }
  return truncate(JSON.stringify(value) ?? "null");
}

const MAX_EXPRESSION = 96;

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

function expressionText(when: unknown): string {
  if (!isRecord(when)) return operandSummary(when);
  if (Array.isArray(when.all)) return `all(${when.all.map(expressionText).join(", ")})`;
  if (Array.isArray(when.any)) return `any(${when.any.map(expressionText).join(", ")})`;
  if ("not" in when && !("op" in when)) return `not(${expressionText(when.not)})`;
  if (when.op === "exists") return `${operandSummary(when.left)} exists`;
  if (typeof when.op === "string") return `${operandSummary(when.left)} ${when.op} ${operandSummary(when.right ?? null)}`;
  return operandSummary(when);
}

/**
 * A condition as text: a comparison is "left op right" (exactly as before the loops milestone), a
 * combinator is `all(…)`, `any(…)` or `not(…)` over its operands. A combinator's text is capped at
 * MAX_EXPRESSION characters.
 */
export function conditionSummary(when: Expression): string {
  const text = expressionText(when);
  return isRecord(when) && typeof when.op === "string" ? text : truncate(text, MAX_EXPRESSION);
}

/** Every hand-off of a step, in declaration order (a parallel's join last). */
export function stepEdges(step: WorkflowStep): GraphEdge[] {
  const edge = (target: string, kind: EdgeKind, index: number, label?: string): GraphEdge => ({
    id: `${step.id}->${target}:${kind}:${index}`,
    source: step.id,
    target,
    kind,
    ...(label !== undefined ? { label } : {}),
  });
  switch (step.type) {
    case "branch":
      return [
        ...step.cases.map((c, i) => edge(c.next, "case", i, `${i + 1}: ${conditionSummary(c.when)}`)),
        edge(step.default, "default", 0, "default"),
      ];
    case "parallel":
      return [...step.branches.map((b, i) => edge(b, "fork", i)), edge(step.join, "join", 0, "join")];
    default:
      return step.next ? [edge(step.next, "next", 0)] : [];
  }
}

export function stepTargets(step: WorkflowStep): string[] {
  return stepEdges(step).map((e) => e.target);
}

export interface BuildGraphOptions {
  /** Loop steps (foreach and loop) drawn as one node with a badge instead of a group with its body. */
  collapsed?: ReadonlySet<string>;
}

interface LevelLayout {
  /** Every node at this level and below; this level's own positions are relative to the level's origin. */
  nodes: GraphNode[];
  edges: GraphEdge[];
  width: number;
  height: number;
}

/** Nesting deeper than this (an invalid spec) is drawn collapsed rather than recursed into. */
const MAX_DRAWN_DEPTH = 4;

/**
 * One level of the graph (the top level, or a loop body) laid out in layers. A loop step at this
 * level is a group sized to hold its own body's layout (recursively), unless it is collapsed. With
 * every node the same size this is the original layered layout, number for number.
 */
function layoutLevel(
  steps: readonly WorkflowStep[],
  start: string,
  parentId: string | null,
  depth: number,
  claimed: Set<string>,
  collapsed: ReadonlySet<string>,
): LevelLayout {
  const byId = new Map<string, WorkflowStep>();
  for (const step of steps) {
    // A duplicate id (an invalid spec) keeps its first occurrence, at any depth.
    if (!step || typeof step.id !== "string" || claimed.has(step.id)) continue;
    claimed.add(step.id);
    byId.set(step.id, step);
  }
  const edges = [...byId.values()].flatMap(stepEdges).filter((e) => byId.has(e.target));

  // Breadth-first visit order from start, then any unreachable steps in declaration order.
  const visit: string[] = [];
  const seen = new Set<string>();
  const queue = byId.has(start) ? [start] : [];
  while (queue.length > 0) {
    const id = queue.shift()!;
    if (seen.has(id)) continue;
    seen.add(id);
    visit.push(id);
    for (const target of stepTargets(byId.get(id)!)) if (byId.has(target) && !seen.has(target)) queue.push(target);
  }
  for (const id of byId.keys()) if (!seen.has(id)) visit.push(id);

  // Longest path in a DAG: relax in topological order (Kahn, ties broken by visit order).
  const indegree = new Map<string, number>(visit.map((id) => [id, 0]));
  for (const e of edges) indegree.set(e.target, (indegree.get(e.target) ?? 0) + 1);
  const rank = new Map(visit.map((id, i) => [id, i]));
  const ready = visit.filter((id) => indegree.get(id) === 0);
  const layer = new Map<string, number>(visit.map((id) => [id, 0]));
  const done = new Set<string>();
  while (done.size < visit.length) {
    if (ready.length === 0) {
      // A cycle (an invalid spec): break it at the earliest unfinished step.
      ready.push(visit.find((id) => !done.has(id))!);
    }
    ready.sort((a, b) => rank.get(a)! - rank.get(b)!);
    const id = ready.shift()!;
    if (done.has(id)) continue;
    done.add(id);
    for (const e of edges) {
      if (e.source !== id || done.has(e.target)) continue;
      layer.set(e.target, Math.max(layer.get(e.target)!, layer.get(id)! + 1));
      const left = indegree.get(e.target)! - 1;
      indegree.set(e.target, left);
      if (left === 0) ready.push(e.target);
    }
  }
  // Unreachable steps start a layer below everything reachable.
  const reachableDepth = Math.max(0, ...visit.filter((id) => seen.has(id)).map((id) => layer.get(id)!));
  if (visit.some((id) => !seen.has(id))) {
    for (const id of visit) if (!seen.has(id)) layer.set(id, layer.get(id)! + reachableDepth + 1);
  }

  // Each loop step's body, laid out first: it sets the group's size.
  const bodies = new Map<string, LevelLayout>();
  const size = new Map<string, { width: number; height: number }>();
  for (const id of visit) {
    const step = byId.get(id)!;
    const body = isLoopStep(step) && isRecord(step.body) ? step.body : null;
    if (body && !collapsed.has(id) && depth < MAX_DRAWN_DEPTH) {
      const inner = layoutLevel(Array.isArray(body.steps) ? body.steps : [], body.start, id, depth + 1, claimed, collapsed);
      bodies.set(id, inner);
      size.set(id, {
        width: Math.max(GROUP_MIN_WIDTH, inner.width + 2 * GROUP_PAD_X),
        height: GROUP_HEADER_HEIGHT + Math.max(inner.height, 0) + GROUP_PAD_BOTTOM,
      });
    } else {
      size.set(id, { width: NODE_WIDTH, height: NODE_HEIGHT });
    }
  }

  const layers = new Map<number, string[]>();
  for (const id of visit) {
    const l = layer.get(id)!;
    if (!layers.has(l)) layers.set(l, []);
    layers.get(l)!.push(id);
  }
  const rowWidth = (ids: string[]) => ids.reduce((sum, id) => sum + size.get(id)!.width, 0) + (ids.length - 1) * H_GAP;
  const rowHeight = (l: number) => Math.max(NODE_HEIGHT, ...(layers.get(l) ?? []).map((id) => size.get(id)!.height));
  const sorted = [...layers.entries()].sort((a, b) => a[0] - b[0]);
  const totalWidth = Math.max(NODE_WIDTH, ...sorted.map(([, ids]) => rowWidth(ids)));
  // A layer's top: every layer above it (an empty one counts as one plain node high).
  const maxLayer = sorted.length > 0 ? sorted[sorted.length - 1][0] : -1;
  const tops: number[] = [];
  let y = 0;
  for (let l = 0; l <= maxLayer; l += 1) {
    tops.push(y);
    y += rowHeight(l) + V_GAP;
  }
  const height = maxLayer < 0 ? 0 : tops[maxLayer] + rowHeight(maxLayer);

  const nodes: GraphNode[] = [];
  const allEdges = [...edges];
  for (const [l, ids] of sorted) {
    let x = (totalWidth - rowWidth(ids)) / 2;
    ids.forEach((id, order) => {
      const step = byId.get(id)!;
      const { width, height: h } = size.get(id)!;
      const inner = bodies.get(id);
      nodes.push({
        id,
        step,
        layer: l,
        order,
        x,
        y: tops[l],
        parentId,
        depth,
        width,
        height: h,
        kind: inner ? "group" : "step",
        collapsed: isLoopStep(step) && !inner,
      });
      if (inner) {
        // The body's own nodes sit inside the group, below its header and centred in it.
        const dx = (width - inner.width) / 2;
        for (const child of inner.nodes) {
          nodes.push(child.parentId === id ? { ...child, x: child.x + dx, y: child.y + GROUP_HEADER_HEIGHT } : child);
        }
        allEdges.push(...inner.edges);
      }
      x += width + H_GAP;
    });
  }
  return { nodes, edges: allEdges, width: maxLayer < 0 ? 0 : totalWidth, height };
}

/**
 * Build the graph and its layout. Tolerates a malformed spec (unknown targets are dropped,
 * unreachable steps go below the reachable ones, a cycle cannot loop forever, an unknown step kind
 * is a plain node).
 *
 * A loop step (foreach or loop) is a group node holding its body's own layered layout; the body's
 * nodes have `parentId` set to the loop's id and positions relative to the group (React Flow's
 * sub-flow convention), and come right after their group in `nodes`. A loop in `collapsed` is one
 * plain-sized node instead, and its body is left out. A spec with no loops lays out exactly as
 * before the loops milestone.
 */
export function buildWorkflowGraph(spec: WorkflowSpec, options: BuildGraphOptions = {}): WorkflowGraph {
  const { nodes, edges } = layoutLevel(spec.steps ?? [], spec.start, null, 0, new Set(), options.collapsed ?? new Set());
  return { nodes, edges };
}

/** Every loop step (foreach or loop) at any depth, in declaration order (outer before inner). */
export function loopStepIds(spec: WorkflowSpec): string[] {
  const ids: string[] = [];
  const walk = (steps: readonly WorkflowStep[] | undefined, depth: number) => {
    if (!Array.isArray(steps) || depth > MAX_DRAWN_DEPTH) return;
    for (const step of steps) {
      if (!step || !isLoopStep(step)) continue;
      ids.push(step.id);
      walk(isRecord(step.body) ? step.body.steps : undefined, depth + 1);
    }
  };
  walk(spec.steps, 0);
  return ids;
}

/** The loop's bound as a badge: "×50" (the most items or iterations it may run). */
export function loopBadge(step: LoopKindStep): string {
  const max = step.type === "foreach" ? step.max_items : step.max_iterations;
  return `×${max}`;
}

/**
 * What a loop step does, one short line each: for foreach the items it walks, its bound,
 * concurrency and error rule; for loop its while/until condition, its bound and what happens at it.
 */
export function loopSummary(step: LoopKindStep): string[] {
  if (step.type === "foreach") {
    const concurrency = step.concurrency ?? 1;
    const ref = isRecord(step.items) && typeof step.items.$ref === "string" ? truncate(step.items.$ref) : "items";
    return [
      `each of ${ref}`,
      [
        `max ${step.max_items}`,
        concurrency > 1 ? `${concurrency} at a time` : "one at a time",
        (step.on_item_error ?? "fail") === "collect" ? "collect errors" : "stop on error",
      ].join(" · "),
    ];
  }
  const condition = step.until !== undefined && step.until !== null
    ? `until ${conditionSummary(step.until)}`
    : step.while !== undefined && step.while !== null
      ? `while ${conditionSummary(step.while)}`
      : "no condition";
  return [
    condition,
    [`max ${step.max_iterations}`, (step.on_max_iterations ?? "fail") === "exit" ? "exit at max" : "fail at max", ...(step.state ? ["state"] : [])].join(" · "),
  ];
}

/** Steps reachable from `roots` (roots included), never entering `stop`. */
export function reachableFrom(spec: WorkflowSpec, roots: string[], stop?: string): Set<string> {
  const byId = new Map(spec.steps.map((s) => [s.id, s]));
  const seen = new Set<string>();
  const todo = roots.filter((r) => r !== stop);
  while (todo.length > 0) {
    const id = todo.pop()!;
    if (seen.has(id) || !byId.has(id)) continue;
    seen.add(id);
    for (const t of stepTargets(byId.get(id)!)) if (t !== stop && !seen.has(t)) todo.push(t);
  }
  return seen;
}
