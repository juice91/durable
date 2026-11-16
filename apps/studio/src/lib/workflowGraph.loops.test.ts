import { describe, expect, it } from "vitest";
import {
  buildWorkflowGraph,
  conditionSummary,
  GROUP_HEADER_HEIGHT,
  GROUP_PAD_X,
  loopBadge,
  loopStepIds,
  loopSummary,
  NODE_HEIGHT,
  NODE_WIDTH,
  type GraphNode,
} from "./workflowGraph";
import type { ForeachStep, LoopStep, WorkflowSpec } from "./workflows";
import { demoSpec, parallelSpec, parallelApprovalSpec } from "@/test/workflowFixtures";
import { foreachSpec, nestedSpec, refineSpec, untilSpec } from "@/test/workflowLoopFixtures";

const byId = (nodes: GraphNode[]) => Object.fromEntries(nodes.map((n) => [n.id, n]));

describe("workflow graph: loop groups (LP.6)", () => {
  it("keeps loop-free specs exactly as before: every node plain, top level, 200x64 on the old grid", () => {
    for (const spec of [demoSpec, parallelSpec, parallelApprovalSpec]) {
      const g = buildWorkflowGraph(spec);
      for (const n of g.nodes) {
        expect(n).toMatchObject({ parentId: null, depth: 0, width: NODE_WIDTH, height: NODE_HEIGHT, kind: "step", collapsed: false });
        expect(n.y).toBe(n.layer * (NODE_HEIGHT + 70));
      }
    }
    // The old x formula: centred rows on the widest layer.
    const g = byId(buildWorkflowGraph(parallelSpec).nodes);
    expect(g.fork.x).toBe((2 * NODE_WIDTH + 60 - NODE_WIDTH) / 2);
    expect([g.a.x, g.b.x]).toEqual([0, NODE_WIDTH + 60]);
  });

  it("encloses a foreach body in its group, children after the parent, positions relative", () => {
    const g = buildWorkflowGraph(foreachSpec);
    expect(g.nodes.map((n) => n.id)).toEqual(["fetch", "each", "triage", "update", "report"]);
    const n = byId(g.nodes);
    expect(n.each).toMatchObject({ kind: "group", parentId: null, depth: 0 });
    for (const id of ["triage", "update"]) {
      const child = n[id];
      expect(child).toMatchObject({ parentId: "each", depth: 1, kind: "step" });
      expect(child.x).toBeGreaterThanOrEqual(GROUP_PAD_X);
      expect(child.y).toBeGreaterThanOrEqual(GROUP_HEADER_HEIGHT);
      expect(child.x + child.width).toBeLessThanOrEqual(n.each.width - GROUP_PAD_X);
      expect(child.y + child.height).toBeLessThanOrEqual(n.each.height);
    }
    expect(n.triage.y).toBeLessThan(n.update.y);
    // The group is taller than a node, and the step after it sits below the whole group.
    expect(n.each.height).toBeGreaterThan(2 * NODE_HEIGHT);
    expect(n.report.y).toBeGreaterThanOrEqual(n.each.y + n.each.height);
    expect(g.edges.map((e) => `${e.source}->${e.target}`)).toEqual(["fetch->each", "each->report", "triage->update"]);
  });

  it("nests a loop group inside a foreach group (depth 2) and keeps every child inside its parent", () => {
    const g = buildWorkflowGraph(nestedSpec);
    const n = byId(g.nodes);
    expect(n.inner).toMatchObject({ kind: "group", parentId: "outer", depth: 1 });
    expect(n.leaf).toMatchObject({ parentId: "inner", depth: 2 });
    expect(n.tally).toMatchObject({ parentId: "outer", depth: 1 });
    for (const child of g.nodes.filter((c) => c.parentId)) {
      const parent = n[child.parentId!];
      expect(child.x).toBeGreaterThanOrEqual(0);
      expect(child.y).toBeGreaterThanOrEqual(GROUP_HEADER_HEIGHT);
      expect(child.x + child.width).toBeLessThanOrEqual(parent.width);
      expect(child.y + child.height).toBeLessThanOrEqual(parent.height);
      // React Flow needs the parent first.
      expect(g.nodes.indexOf(parent)).toBeLessThan(g.nodes.indexOf(child));
    }
    expect(n.outer.width).toBeGreaterThan(n.inner.width);
  });

  it("is deterministic", () => {
    expect(buildWorkflowGraph(nestedSpec)).toEqual(buildWorkflowGraph(nestedSpec));
  });

  it("collapses a loop to one plain node and leaves its body out", () => {
    const g = buildWorkflowGraph(foreachSpec, { collapsed: new Set(["each"]) });
    expect(g.nodes.map((n) => n.id)).toEqual(["fetch", "each", "report"]);
    const n = byId(g.nodes);
    expect(n.each).toMatchObject({ kind: "step", collapsed: true, width: NODE_WIDTH, height: NODE_HEIGHT });
    expect(g.edges.some((e) => e.source === "triage")).toBe(false);
    expect(n.report.y).toBe(2 * (NODE_HEIGHT + 70));
    // Collapsing the inner loop only hides its own body.
    const nested = byId(buildWorkflowGraph(nestedSpec, { collapsed: new Set(["inner"]) }).nodes);
    expect(nested.leaf).toBeUndefined();
    expect(nested.inner).toMatchObject({ parentId: "outer", collapsed: true });
    expect(nested.tally).toBeDefined();
  });

  it("lists every loop id, outer first", () => {
    expect(loopStepIds(nestedSpec)).toEqual(["outer", "inner"]);
    expect(loopStepIds(demoSpec)).toEqual([]);
  });

  it("badges and summarises loops", () => {
    const each = foreachSpec.steps[1] as ForeachStep;
    const poll = untilSpec.steps[0] as LoopStep;
    expect(loopBadge(each)).toBe("×50");
    expect(loopBadge(poll)).toBe("×10");
    expect(loopSummary(each)).toEqual(["each of steps.fetch.output.tickets", "max 50 · 4 at a time · stop on error"]);
    expect(loopSummary(poll)).toEqual(["until steps.check.output.done eq true", "max 10 · fail at max · state"]);
    const inner = (nestedSpec.steps[1] as ForeachStep).body.steps[0] as LoopStep;
    expect(loopSummary(inner)[0]).toBe("while all(loop.inner.index lt 3, not(loop.inner.state.stop exists))");
    expect(loopSummary(nestedSpec.steps[1] as ForeachStep)[1]).toBe("max 20 · one at a time · collect errors");
    expect(loopSummary((refineSpec.steps[0] as LoopStep))[1]).toBe("max 3 · exit at max · state");
  });

  it("summarises combinators, leaves a bare comparison unchanged, caps long ones", () => {
    expect(conditionSummary({ op: "eq", left: { $ref: "input.urgent" }, right: true })).toBe("input.urgent eq true");
    expect(conditionSummary({ any: [{ op: "eq", left: { $ref: "input.a" }, right: 1 }, { op: "exists", left: { $ref: "input.b" } }] })).toBe("any(input.a eq 1, input.b exists)");
    expect(conditionSummary({ not: { op: "ne", left: { $ref: "input.a" }, right: "x" } })).toBe('not(input.a ne "x")');
    const long = { all: Array.from({ length: 8 }, (_, i) => ({ op: "eq" as const, left: { $ref: `steps.step_${i}.output.value` }, right: i })) };
    const text = conditionSummary(long);
    expect(text.length).toBeLessThanOrEqual(96);
    expect(text.endsWith("…")).toBe(true);
    const edges = buildWorkflowGraph(nestedSpec).edges.filter((e) => e.kind === "case");
    expect(edges.map((e) => e.label)).toEqual(["1: any(input.a eq 1, input.b exists)"]);
  });

  it("tolerates unknown kinds, a malformed body, duplicate ids across depths and runaway nesting", () => {
    const spec = {
      spec_version: 1,
      name: "Odd",
      input_schema: { type: "object" },
      start: "a",
      steps: [
        { id: "a", type: "future_kind", next: "l" },
        { id: "l", type: "loop", until: { op: "exists", left: { $ref: "x" } }, max_iterations: 2, body: { start: "a", steps: [{ id: "a", type: "tool", tool_id: "t" }] }, next: "m" },
        { id: "m", type: "foreach", items: { $ref: "input.x" }, max_items: 1, body: null },
      ],
    } as unknown as WorkflowSpec;
    const g = buildWorkflowGraph(spec);
    expect(g.nodes.map((n) => n.id)).toEqual(["a", "l", "m"]);
    const n = byId(g.nodes);
    expect(n.a.kind).toBe("step");
    expect(n.l).toMatchObject({ kind: "group" }); // its only body step was a duplicate id, dropped
    expect(n.m).toMatchObject({ kind: "step", collapsed: true });

    // Ten levels deep (invalid): drawn down to the cap, then collapsed, never recursing forever.
    let body: unknown = { start: "x9", steps: [{ id: "x9", type: "tool", tool_id: "t" }] };
    for (let i = 8; i >= 0; i -= 1) {
      body = { start: `x${i}`, steps: [{ id: `x${i}`, type: "loop", until: { op: "exists", left: { $ref: "y" } }, max_iterations: 1, body }] };
    }
    const deep = { ...spec, start: "x0", steps: (body as { steps: unknown[] }).steps } as unknown as WorkflowSpec;
    const dg = buildWorkflowGraph(deep);
    expect(Math.max(...dg.nodes.map((d) => d.depth))).toBe(4);
    expect(dg.nodes.at(-1)).toMatchObject({ collapsed: true });
  });
});
