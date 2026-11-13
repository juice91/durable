import { describe, expect, it } from "vitest";
import { buildWorkflowGraph, conditionSummary, stepEdges } from "./workflowGraph";
import type { WorkflowSpec } from "./workflows";
import { demoSpec, parallelSpec } from "@/test/workflowFixtures";

describe("workflow graph", () => {
  it("labels branch cases with the condition and the default", () => {
    const route = demoSpec.steps.find((s) => s.id === "route")!;
    const edges = stepEdges(route);
    expect(edges.map((e) => [e.target, e.kind, e.label])).toEqual([
      ["approve", "case", "1: input.urgent eq true"],
      ["summarize", "default", "default"],
    ]);
  });

  it("summarizes operands: refs as paths, literals as compact JSON, exists without right", () => {
    expect(conditionSummary({ op: "contains", left: { $ref: "steps.lookup.output.tags" }, right: "vip" })).toBe('steps.lookup.output.tags contains "vip"');
    expect(conditionSummary({ op: "exists", left: { $ref: "input.x" } })).toBe("input.x exists");
    expect(conditionSummary({ op: "eq", left: { $ref: "input.o" }, right: { a: "a".repeat(80) } }).length).toBeLessThan(80);
  });

  it("lays out by longest path from start, deterministically", () => {
    const g1 = buildWorkflowGraph(demoSpec);
    const g2 = buildWorkflowGraph(demoSpec);
    expect(g1).toEqual(g2);
    const layer = Object.fromEntries(g1.nodes.map((n) => [n.id, n.layer]));
    // summarize is reachable from route directly but sits below approve (longest path).
    expect(layer).toEqual({ classify: 0, lookup: 1, route: 2, approve: 3, summarize: 4 });
    const ys = g1.nodes.map((n) => n.y);
    expect(new Set(ys).size).toBe(5);
  });

  it("forks parallel branches and places the join below every branch", () => {
    const g = buildWorkflowGraph(parallelSpec);
    const byId = Object.fromEntries(g.nodes.map((n) => [n.id, n]));
    expect(byId.a.layer).toBe(1);
    expect(byId.b.layer).toBe(1);
    expect(byId.merge.layer).toBe(3);
    expect(byId.a.x).not.toBe(byId.b.x);
    expect(g.edges.filter((e) => e.kind === "fork").map((e) => e.target)).toEqual(["a", "b"]);
    expect(g.edges.find((e) => e.kind === "join")).toMatchObject({ source: "fork", target: "merge", label: "join" });
  });

  it("tolerates unknown targets, unreachable steps and cycles without looping", () => {
    const bad = {
      ...demoSpec,
      start: "x",
      steps: [
        { id: "x", type: "tool", tool_id: "t", next: "y" },
        { id: "y", type: "tool", tool_id: "t", next: "x" },
        { id: "z", type: "tool", tool_id: "t", next: "missing" },
      ],
    } as WorkflowSpec;
    const g = buildWorkflowGraph(bad);
    expect(g.nodes.map((n) => n.id).sort()).toEqual(["x", "y", "z"]);
    expect(g.edges.some((e) => e.target === "missing")).toBe(false);
    const z = g.nodes.find((n) => n.id === "z")!;
    expect(z.layer).toBeGreaterThan(Math.max(...g.nodes.filter((n) => n.id !== "z").map((n) => n.layer)));
  });
});
