// A workflow spec as a graph: one node per step, one edge per hand-off, and a deterministic layered
// layout (layer = longest path from `start`, order within a layer = first visit in a breadth-first
// walk that follows each step's targets in declaration order). Pure; no React Flow types here.
import type { Condition, WorkflowSpec, WorkflowStep } from "./workflows";

export type EdgeKind = "next" | "case" | "default" | "fork" | "join";

export interface GraphNode {
  id: string;
  step: WorkflowStep;
  layer: number;
  order: number;
  x: number;
  y: number;
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

export function conditionSummary(when: Condition): string {
  if (when.op === "exists") return `${operandSummary(when.left)} exists`;
  return `${operandSummary(when.left)} ${when.op} ${operandSummary(when.right ?? null)}`;
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

/**
 * Build the graph and its layout. Tolerates a malformed spec (unknown targets are dropped,
 * unreachable steps go below the reachable ones, a cycle cannot loop forever).
 */
export function buildWorkflowGraph(spec: WorkflowSpec): WorkflowGraph {
  const byId = new Map<string, WorkflowStep>();
  for (const step of spec.steps) if (!byId.has(step.id)) byId.set(step.id, step);
  const edges = [...byId.values()].flatMap(stepEdges).filter((e) => byId.has(e.target));

  // Breadth-first visit order from start, then any unreachable steps in declaration order.
  const visit: string[] = [];
  const seen = new Set<string>();
  const queue = byId.has(spec.start) ? [spec.start] : [];
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

  const layers = new Map<number, string[]>();
  for (const id of visit) {
    const l = layer.get(id)!;
    if (!layers.has(l)) layers.set(l, []);
    layers.get(l)!.push(id);
  }
  const widest = Math.max(1, ...[...layers.values()].map((ids) => ids.length));
  const totalWidth = widest * NODE_WIDTH + (widest - 1) * H_GAP;
  const nodes: GraphNode[] = [];
  for (const [l, ids] of [...layers.entries()].sort((a, b) => a[0] - b[0])) {
    const rowWidth = ids.length * NODE_WIDTH + (ids.length - 1) * H_GAP;
    const offset = (totalWidth - rowWidth) / 2;
    ids.forEach((id, order) => {
      nodes.push({
        id,
        step: byId.get(id)!,
        layer: l,
        order,
        x: offset + order * (NODE_WIDTH + H_GAP),
        y: l * (NODE_HEIGHT + V_GAP),
      });
    });
  }
  return { nodes, edges };
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
