// LP.6 review: Timeline rows group a loop's body under the loop, indented as in the step list.
import { describe, expect, it } from "vitest";
import type { RunEvent } from "@/lib/runs";
import { deriveRunView } from "@/lib/workflowRunState";
import { demoSpec } from "@/test/workflowFixtures";
import { nestedSpec } from "@/test/workflowLoopFixtures";
import { timelineRows } from "./workflowTimeline";

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
