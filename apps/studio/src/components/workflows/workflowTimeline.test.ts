import { describe, expect, it } from "vitest";
import type { RunEvent } from "@/lib/runs";
import { deriveRunView } from "@/lib/workflowRunState";
import { demoSpec } from "@/test/workflowFixtures";
import { timelineClock, timelineRows, timelineSummary } from "./workflowTimeline";

let seq = 0;
const ev = (type: RunEvent["type"], data: Record<string, unknown>, ts: string): RunEvent => ({ seq: seq++, type, data, ts });

describe("timelineRows", () => {
  it("has one row per step with the step's state, started steps first in start order", () => {
    seq = 0;
    const events = [
      ev("step_started", { step_id: "classify", step_type: "agent" }, "2026-11-13T10:00:00.000Z"),
      ev("step_completed", { step_id: "classify", output: "billing" }, "2026-11-13T10:00:02.000Z"),
      ev("step_started", { step_id: "lookup", step_type: "tool" }, "2026-11-13T10:00:02.500Z"),
    ];
    const view = deriveRunView(demoSpec, events, "running");
    const rows = timelineRows(demoSpec, view, timelineClock(view, null, new Date("2026-11-13T10:00:04.000Z").getTime()));

    expect(rows.map((row) => row.stepId)).toEqual(["classify", "lookup", "route", "approve", "summarize"]);
    for (const row of rows) expect(row.state).toBe(view.steps[row.stepId].state);
    expect(rows[0]).toMatchObject({ state: "completed", durationMs: 2000, stepType: "agent" });
    expect(rows[1]).toMatchObject({ state: "running", durationMs: null });
    expect(rows[2]).toMatchObject({ state: "pending", bar: null, durationMs: null });
    // The bars are placed within the run's span (10:00:00 to "now", 10:00:04).
    expect(rows[0].bar).toEqual({ offset: 0, width: 0.5 });
    expect(rows[1].bar?.offset).toBeCloseTo(0.625);
    expect(timelineSummary(rows)).toEqual({ done: 1, total: 5 });
  });

  it("counts skipped steps as done and returns nothing without a spec", () => {
    seq = 0;
    const events = [
      ev("step_started", { step_id: "route", step_type: "branch" }, "2026-11-13T10:00:00Z"),
      ev("step_completed", { step_id: "route", output: { case: null, next: "summarize" } }, "2026-11-13T10:00:01Z"),
    ];
    const view = deriveRunView(demoSpec, events, "running");
    const rows = timelineRows(demoSpec, view, Date.now());
    expect(rows.find((row) => row.stepId === "approve")?.state).toBe("skipped");
    expect(timelineSummary(rows).done).toBe(2);
    expect(timelineRows(null, view, Date.now())).toEqual([]);
  });

  it("closes a step the run interrupted at the run's end, not at the wall clock", () => {
    seq = 0;
    const events = [
      ev("step_started", { step_id: "classify", step_type: "agent" }, "2026-11-13T10:00:00Z"),
      ev("step_completed", { step_id: "classify", output: "x" }, "2026-11-13T10:00:01Z"),
      ev("step_started", { step_id: "lookup", step_type: "tool" }, "2026-11-13T10:00:02Z"),
      ev("run_failed", { status: "failed", error: "worker lost" }, "2026-11-13T10:00:06Z"),
    ];
    const view = deriveRunView(demoSpec, events, "failed");
    const soon = timelineRows(demoSpec, view, timelineClock(view, "2026-11-13T10:00:07Z", Date.parse("2026-11-13T10:00:08Z")));
    const muchLater = timelineRows(demoSpec, view, timelineClock(view, "2026-11-13T10:00:07Z", Date.parse("2030-01-01T00:00:00Z")));
    expect(muchLater).toEqual(soon);
    const lookup = soon.find((row) => row.stepId === "lookup")!;
    expect(lookup.state).toBe("failed");
    // Interrupted at the terminal event (10:00:06): 4 s, ending the run's span.
    expect(lookup.durationMs).toBe(4000);
    expect(lookup.bar!.offset + lookup.bar!.width).toBeCloseTo(1);
    expect(lookup.bar!.offset).toBeCloseTo(2 / 6);
  });

  it("uses the run row's updated_at when no terminal event arrived, and now for a live run", () => {
    seq = 0;
    const started = [ev("step_started", { step_id: "classify", step_type: "agent" }, "2026-11-13T10:00:00Z")];
    const ended = deriveRunView(demoSpec, started, "failed");
    expect(timelineClock(ended, "2026-11-13T10:00:03Z", Date.parse("2030-01-01T00:00:00Z"))).toBe(Date.parse("2026-11-13T10:00:03Z"));
    const live = deriveRunView(demoSpec, started, "running");
    expect(timelineClock(live, "2026-11-13T10:00:03Z", 12345)).toBe(12345);
    const row = timelineRows(demoSpec, live, Date.parse("2026-11-13T10:00:05Z")).find((r) => r.stepId === "classify")!;
    expect(row.durationMs).toBeNull();
  });
});
