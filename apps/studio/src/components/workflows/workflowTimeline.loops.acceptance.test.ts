// LP.12 acceptance: Timeline iteration rows and depths, loop-free goldens, summary.
import { describe, expect, it } from "vitest";
import type { RunEvent } from "@/lib/runs";
import { deriveRunView, resolveRunStates } from "@/lib/workflowRunState";
import { demoSpec, parallelApprovalSpec, parallelSpec } from "@/test/workflowFixtures";
import { foreachSpec, nestedSpec, untilSpec } from "@/test/workflowLoopFixtures";
import { timelineRows, timelineSummary } from "./workflowTimeline";

let seq = 0;
const ev = (type: string, data: Record<string, unknown>, ts: string): RunEvent => ({ seq: seq++, type: type as RunEvent["type"], data, ts });
const sc = (...frames: [string, number][]) => frames.map(([loop, index]) => ({ loop, index }));
const CLOCK = new Date("2026-11-15T10:05:00Z").getTime();
const shape = (rows: ReturnType<typeof timelineRows>) => rows.map((r) => [r.key, r.kind, r.depth, r.parentId]);

describe("loop-free timelines are unchanged (golden)", () => {
  it("demoSpec, parallelSpec and parallelApprovalSpec: flat, depth 0, kind step, no parent", () => {
    const golden: [typeof demoSpec, string[]][] = [
      [demoSpec, ["classify", "lookup", "route", "approve", "summarize"]],
      [parallelSpec, ["fork", "a", "a2", "b", "merge"]],
      [parallelApprovalSpec, ["fork", "gate", "b", "merge"]],
    ];
    for (const [spec, ids] of golden) {
      const rows = timelineRows(spec, deriveRunView(spec, [], "queued"), CLOCK);
      expect(rows.map((r) => r.stepId).sort()).toEqual([...ids].sort());
      expect(rows.every((r) => r.kind === "step" && r.depth === 0 && r.parentId === null && r.iteration === undefined)).toBe(true);
      expect(rows.map((r) => r.key)).toEqual(rows.map((r) => r.stepId));
    }
  });

  it("orders started steps by start time, then the rest in graph order, exactly as before", () => {
    seq = 0;
    const events = [
      ev("step_started", { step_id: "classify", step_type: "agent" }, "2026-11-15T10:00:00Z"),
      ev("step_completed", { step_id: "classify", output: "x" }, "2026-11-15T10:00:02Z"),
      ev("step_started", { step_id: "lookup", step_type: "tool" }, "2026-11-15T10:00:03Z"),
    ];
    const rows = timelineRows(demoSpec, deriveRunView(demoSpec, events, "running"), CLOCK);
    expect(shape(rows)).toEqual([
      ["classify", "step", 0, null],
      ["lookup", "step", 0, null],
      ["route", "step", 0, null],
      ["approve", "step", 0, null],
      ["summarize", "step", 0, null],
    ]);
    expect(rows[0].durationMs).toBe(2000);
    expect(timelineSummary(rows)).toEqual({ done: 1, total: 5 });
  });
});

describe("iteration rows and depths", () => {
  it("a loop with no iterations keeps its body right under it (old order and depths)", () => {
    const rows = timelineRows(untilSpec, deriveRunView(untilSpec, [], "queued"), CLOCK);
    expect(shape(rows)).toEqual([["poll", "step", 0, null], ["check", "step", 1, "poll"]]);
  });

  it("nested loops: outer iterations depth 1, shown outer body depth 2, inner iterations depth 3, inner body depth 4", () => {
    seq = 0;
    const t = "2026-11-15T10:00:00Z";
    const view = deriveRunView(nestedSpec, [
      ev("loop_iteration_started", { step_id: "outer", scope: [], index: 0 }, t),
      ev("loop_iteration_completed", { step_id: "outer", scope: [], index: 0 }, t),
      ev("loop_iteration_started", { step_id: "outer", scope: [], index: 1 }, t),
      ev("loop_iteration_started", { step_id: "inner", scope: sc(["outer", 1]), index: 0 }, t),
      ev("loop_iteration_started", { step_id: "inner", scope: sc(["outer", 1]), index: 1 }, t),
      ev("step_started", { step_id: "leaf", step_type: "tool", scope: sc(["outer", 1], ["inner", 1]) }, t),
    ], "running");
    const rows = timelineRows(nestedSpec, view, CLOCK);
    const outer = rows.findIndex((r) => r.key === "outer");
    const slice = rows.slice(outer, rows.findIndex((r) => r.key === "route"));
    expect(slice.map((r) => [r.key, r.depth])).toEqual([
      ["outer", 0],
      ["iteration:outer.0", 1],
      ["iteration:outer.1", 1],
      ["inner", 2],
      ["iteration:outer.1/inner.0", 3],
      ["iteration:outer.1/inner.1", 3],
      ["leaf", 4],
      ["tally", 2],
    ]);
    expect(slice.filter((r) => r.kind === "iteration").map((r) => r.parentId)).toEqual(["outer", "outer", "inner", "inner"]);
    expect(rows.find((r) => r.key === "iteration:outer.1/inner.1")?.iteration).toMatchObject({ loopId: "inner", index: 1, scope: "outer.1/inner.1", shown: true });
    expect(rows.find((r) => r.key === "iteration:outer.1/inner.0")?.iteration?.shown).toBe(false);
  });

  it("selection moves the body under a different iteration row; ids stay unique", () => {
    seq = 0;
    const t = "2026-11-15T10:00:00Z";
    const events = [0, 1, 2].flatMap((i) => [
      ev("loop_iteration_started", { step_id: "each", scope: [], index: i }, t),
      ev("loop_iteration_completed", { step_id: "each", scope: [], index: i }, t),
    ]);
    const view = deriveRunView(foreachSpec, events, "running");
    for (const pick of [0, 1, 2]) {
      const rows = timelineRows(foreachSpec, view, CLOCK, resolveRunStates(foreachSpec, view, { each: pick }));
      const at = rows.findIndex((r) => r.key === `iteration:each.${pick}`);
      expect(rows[at].iteration?.shown).toBe(true);
      expect(rows[at + 1]).toMatchObject({ stepId: "triage", depth: 2 });
      expect(rows.filter((r) => r.iteration?.shown)).toHaveLength(1);
      expect(new Set(rows.map((r) => r.key)).size).toBe(rows.length);
    }
  });

  it("an iteration row carries its error code and failed state", () => {
    seq = 0;
    const view = deriveRunView(foreachSpec, [
      ev("loop_iteration_started", { step_id: "each", scope: [], index: 0 }, "2026-11-15T10:00:00Z"),
      ev("loop_iteration_failed", { step_id: "each", scope: [], index: 0, status: "failed", error_code: "tool_error" }, "2026-11-15T10:00:04Z"),
    ], "running");
    const row = timelineRows(foreachSpec, view, CLOCK).find((r) => r.kind === "iteration")!;
    expect(row).toMatchObject({ state: "failed", durationMs: 4000, iteration: { errorCode: "tool_error" } });
  });
});

describe("timelineSummary", () => {
  it("counts steps only: iteration rows are in neither done nor total", () => {
    seq = 0;
    const t = "2026-11-15T10:00:00Z";
    const view = deriveRunView(foreachSpec, [
      ev("step_completed", { step_id: "fetch", output: {} }, t),
      ev("loop_iteration_started", { step_id: "each", scope: [], index: 0 }, t),
      ev("loop_iteration_completed", { step_id: "each", scope: [], index: 0 }, t),
    ], "running");
    const rows = timelineRows(foreachSpec, view, CLOCK);
    expect(rows.some((r) => r.kind === "iteration" && r.state === "completed")).toBe(true);
    const { done, total } = timelineSummary(rows);
    expect(total).toBe(rows.filter((r) => r.kind === "step").length);
    // fetch completed; triage and update are skipped in the shown (ended) iteration.
    expect(done).toBe(3);
  });
});
