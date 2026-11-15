// LP.6 review: Timeline rows group a loop's body under the loop, indented as in the step list.
import { describe, expect, it } from "vitest";
import type { RunEvent } from "@/lib/runs";
import { deriveRunView, resolveRunStates } from "@/lib/workflowRunState";
import { demoSpec } from "@/test/workflowFixtures";
import { foreachSpec, nestedSpec } from "@/test/workflowLoopFixtures";
import { timelineRows, timelineSummary } from "./workflowTimeline";

let seq = 0;
const ev = (type: RunEvent["type"], data: Record<string, unknown>, ts: string): RunEvent => ({ seq: seq++, type, data, ts });

describe("timelineRows with loops", () => {
  it("puts body rows right after their loop, with parent and depth, at every level", () => {
    const view = deriveRunView(nestedSpec, [], "queued");
    const rows = timelineRows(nestedSpec, view, Date.now());
    expect(rows.map((r) => [r.stepId, r.parentId, r.depth])).toEqual([
      ["fetch", null, 0],
      ["outer", null, 0],
      ["inner", "outer", 1],
      ["leaf", "inner", 2],
      ["tally", "outer", 1],
      ["route", null, 0],
      ["done", null, 0],
    ]);
  });

  it("orders by start time within each level only, so a body never splits from its loop", () => {
    seq = 0;
    const events = [
      ev("step_started", { step_id: "fetch", step_type: "tool" }, "2026-11-13T10:00:00Z"),
      ev("step_completed", { step_id: "fetch", output: {} }, "2026-11-13T10:00:01Z"),
      ev("step_started", { step_id: "route", step_type: "branch" }, "2026-11-13T10:00:02Z"),
    ];
    const view = deriveRunView(nestedSpec, events, "running");
    const rows = timelineRows(nestedSpec, view, new Date("2026-11-13T10:00:03Z").getTime());
    // route started, outer did not: route moves up, outer keeps its body with it.
    expect(rows.map((r) => r.stepId)).toEqual(["fetch", "route", "outer", "inner", "leaf", "tally", "done"]);
  });

  it("keeps loop-free specs flat at depth 0", () => {
    const rows = timelineRows(demoSpec, deriveRunView(demoSpec, [], "queued"), Date.now());
    expect(rows.every((r) => r.depth === 0 && r.parentId === null)).toBe(true);
  });
});

describe("timelineRows with iterations (LP.12)", () => {
  const sc = (loop: string, index: number) => [{ loop, index }];
  const iteration = (index: number, failed = false): RunEvent[] => [
    ev("loop_iteration_started" as RunEvent["type"], { step_id: "each", scope: [], index }, `2026-11-15T10:00:${String(index % 60).padStart(2, "0")}Z`),
    ev("step_started", { step_id: "triage", step_type: "agent", scope: sc("each", index) }, `2026-11-15T10:00:${String(index % 60).padStart(2, "0")}Z`),
    failed
      ? ev("loop_iteration_failed" as RunEvent["type"], { step_id: "each", scope: [], index, status: "failed", error: "x", error_code: "model_error" }, "2026-11-15T10:01:00Z")
      : ev("loop_iteration_completed" as RunEvent["type"], { step_id: "each", scope: [], index, state: null }, "2026-11-15T10:01:00Z"),
  ];

  it("groups a loop's body rows under its shown iteration only", () => {
    seq = 0;
    const view = deriveRunView(foreachSpec, [ev("step_started", { step_id: "each", step_type: "foreach" }, "2026-11-15T10:00:00Z"), ...iteration(0), ...iteration(1, true), ...iteration(2)], "running");
    const clock = new Date("2026-11-15T10:02:00Z").getTime();
    const latest = timelineRows(foreachSpec, view, clock);
    expect(latest.map((r) => [r.key, r.depth])).toEqual([
      ["each", 0],
      ["iteration:each.0", 1],
      ["iteration:each.1", 1],
      ["iteration:each.2", 1],
      ["triage", 2],
      ["update", 2],
      ["fetch", 0],
      ["report", 0],
    ]);
    expect(latest.find((r) => r.key === "iteration:each.1")).toMatchObject({ kind: "iteration", state: "failed", iteration: { index: 1, shown: false, errorCode: "model_error" } });
    const picked = timelineRows(foreachSpec, view, clock, resolveRunStates(foreachSpec, view, { each: 0 }));
    expect(picked.map((r) => r.key).slice(1, 5)).toEqual(["iteration:each.0", "triage", "update", "iteration:each.1"]);
    expect(picked.find((r) => r.key === "iteration:each.0")?.iteration?.shown).toBe(true);
    // Iteration rows are groups, not steps.
    expect(timelineSummary(picked).total).toBe(5);
  });

  it("renders 100 iterations as 100 group rows plus one body, not 100 bodies", () => {
    seq = 0;
    const events = Array.from({ length: 100 }, (_, i) => iteration(i)).flat();
    const rows = timelineRows(foreachSpec, deriveRunView(foreachSpec, events, "running"), Date.now());
    expect(rows.filter((r) => r.kind === "iteration")).toHaveLength(100);
    expect(rows.filter((r) => r.stepId === "triage")).toHaveLength(1);
    expect(new Set(rows.map((r) => r.key)).size).toBe(rows.length);
  });
});
