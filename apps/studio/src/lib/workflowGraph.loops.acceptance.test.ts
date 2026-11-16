// LP.6 tester acceptance: layout goldens, nesting, collapse stability.
import { describe, expect, it } from "vitest";
import { buildWorkflowGraph, loopStepIds, type GraphNode } from "./workflowGraph";
import { demoSpec, parallelSpec, parallelApprovalSpec } from "@/test/workflowFixtures";
import { foreachSpec, nestedSpec, refineSpec, untilSpec } from "@/test/workflowLoopFixtures";

const pos = (nodes: GraphNode[]) => nodes.map((n) => [n.id, n.x, n.y]);
const byId = (nodes: GraphNode[]) => Object.fromEntries(nodes.map((n) => [n.id, n]));
const edgeIds = (spec: Parameters<typeof buildWorkflowGraph>[0], c?: Set<string>) =>
  buildWorkflowGraph(spec, c ? { collapsed: c } : {}).edges.map((e) => e.id);

describe("LP.6 golden layout of loop-free specs (captured from the pre-loops builder)", () => {
  it("demoSpec", () => {
    const g = buildWorkflowGraph(demoSpec);
    expect(pos(g.nodes)).toEqual([["classify", 0, 0], ["lookup", 0, 134], ["route", 0, 268], ["approve", 0, 402], ["summarize", 0, 536]]);
    expect(g.edges.map((e) => [e.id, e.kind, e.label])).toEqual([
      ["classify->lookup:next:0", "next", undefined],
      ["lookup->route:next:0", "next", undefined],
      ["route->approve:case:0", "case", "1: input.urgent eq true"],
      ["route->summarize:default:0", "default", "default"],
      ["approve->summarize:next:0", "next", undefined],
    ]);
  });

  it("parallelSpec", () => {
    const g = buildWorkflowGraph(parallelSpec);
    expect(pos(g.nodes)).toEqual([["fork", 130, 0], ["a", 0, 134], ["b", 260, 134], ["a2", 130, 268], ["merge", 130, 402]]);
    expect(g.edges.map((e) => e.id)).toEqual([
      "fork->a:fork:0", "fork->b:fork:1", "fork->merge:join:0", "a->a2:next:0", "a2->merge:next:0", "b->merge:next:0",
    ]);
  });

  it("parallelApprovalSpec", () => {
    const g = buildWorkflowGraph(parallelApprovalSpec);
    expect(pos(g.nodes)).toEqual([["fork", 130, 0], ["gate", 0, 134], ["b", 260, 134], ["merge", 130, 268]]);
    expect(g.edges.map((e) => e.id)).toEqual([
      "fork->gate:fork:0", "fork->b:fork:1", "fork->merge:join:0", "gate->merge:next:0", "b->merge:next:0",
    ]);
  });

  it("a collapsed set naming a non-loop step changes nothing on a loop-free spec", () => {
    expect(buildWorkflowGraph(demoSpec, { collapsed: new Set(["classify"]) })).toEqual(buildWorkflowGraph(demoSpec));
  });
});

describe("LP.6 nested groups and collapse", () => {
  it("lists loops outer-first at depth 2 and draws groups inside groups", () => {
    expect(loopStepIds(nestedSpec)).toEqual(["outer", "inner"]);
    const n = byId(buildWorkflowGraph(nestedSpec).nodes);
    expect([n.outer.depth, n.inner.depth, n.leaf.depth]).toEqual([0, 1, 2]);
    expect([n.outer.kind, n.inner.kind, n.leaf.kind]).toEqual(["group", "group", "step"]);
    expect(n.outer.height).toBeGreaterThan(n.inner.height);
  });

  it("collapsing the inner group drops only its body and shrinks the outer", () => {
    const open = buildWorkflowGraph(nestedSpec);
    const shut = buildWorkflowGraph(nestedSpec, { collapsed: new Set(["inner"]) });
    expect(shut.nodes.map((x) => x.id)).not.toContain("leaf");
    const n = byId(shut.nodes);
    expect(n.inner).toMatchObject({ kind: "step", collapsed: true, width: 200, height: 64, parentId: "outer" });
    expect(n.outer.height).toBeLessThan(byId(open.nodes).outer.height);
    expect(n.tally.parentId).toBe("outer");
  });

  it("collapsing the outer group hides every descendant, even if the inner is not in the set", () => {
    const g = buildWorkflowGraph(nestedSpec, { collapsed: new Set(["outer"]) });
    expect(g.nodes.map((x) => x.id)).toEqual(["fetch", "outer", "route", "done"]);
    expect(g.edges.map((e) => e.source).every((s) => ["fetch", "outer", "route"].includes(s))).toBe(true);
  });

  it("collapsing every loop gives the same graph as collapsing only the outer one (its body is hidden anyway)", () => {
    const all = new Set(loopStepIds(nestedSpec));
    expect(buildWorkflowGraph(nestedSpec, { collapsed: all })).toEqual(buildWorkflowGraph(nestedSpec, { collapsed: new Set(["outer"]) }));
  });

  it("positions are stable after a toggle round trip, and unrelated nodes do not move", () => {
    const before = buildWorkflowGraph(nestedSpec);
    buildWorkflowGraph(nestedSpec, { collapsed: new Set(["inner"]) });
    buildWorkflowGraph(nestedSpec, { collapsed: new Set(["outer", "inner"]) });
    expect(buildWorkflowGraph(nestedSpec)).toEqual(before);
    // The first row is above every loop, so a collapse cannot move it.
    const shut = byId(buildWorkflowGraph(nestedSpec, { collapsed: new Set(["outer"]) }).nodes);
    expect(shut.fetch.y).toBe(byId(before.nodes).fetch.y);
    expect(shut.outer.y).toBe(byId(before.nodes).outer.y);
  });

  it("the same collapsed set always gives identical output regardless of set insertion order", () => {
    const a = buildWorkflowGraph(nestedSpec, { collapsed: new Set(["inner", "outer"]) });
    const b = buildWorkflowGraph(nestedSpec, { collapsed: new Set(["outer", "inner"]) });
    expect(a).toEqual(b);
  });

  it("edges: a collapsed loop keeps its outer edges and loses its body edges", () => {
    expect(edgeIds(foreachSpec)).toEqual(["fetch->each:next:0", "each->report:next:0", "triage->update:next:0"]);
    expect(edgeIds(foreachSpec, new Set(["each"]))).toEqual(["fetch->each:next:0", "each->report:next:0"]);
    expect(edgeIds(untilSpec)).toEqual([]);
    expect(edgeIds(refineSpec)).toEqual(["write->judge:next:0"]);
  });
});
