// LP.12: per-iteration state from scoped events, the pager's resolution, and the cancel flags.
import { describe, expect, it } from "vitest";
import type { RunEvent } from "./runs";
import { adjacentIteration, deriveRunView, findStep, resolveRunStates, scopeString } from "./workflowRunState";
import { foreachSpec, nestedSpec, untilSpec } from "@/test/workflowLoopFixtures";

let seq = 0;
const ev = (type: string, data: Record<string, unknown>, ts = "2026-11-15T10:00:00Z"): RunEvent =>
  ({ seq: seq++, type: type as RunEvent["type"], data, ts });
const sc = (...frames: [string, number][]) => frames.map(([loop, index]) => ({ loop, index }));

/** Iteration `index` of `each` (foreach over tickets: triage → update). */
function eachIteration(index: number, outcome: "completed" | "failed" | "running"): RunEvent[] {
  const scope = sc(["each", index]);
  const out = [
    ev("loop_iteration_started", { step_id: "each", scope: [], index }),
    ev("step_started", { step_id: "triage", step_type: "agent", scope, child_run_id: `child-${index}` }),
  ];
  if (outcome === "running") return out;
  if (outcome === "failed") {
    out.push(ev("step_failed", { step_id: "triage", scope, status: "failed", error: "boom", error_code: "model_error" }));
    out.push(ev("loop_iteration_failed", { step_id: "each", scope: [], index, status: "failed", error: "boom", error_code: "model_error", failed_step_id: "triage" }));
    return out;
  }
  out.push(ev("step_completed", { step_id: "triage", scope, output: `t${index}` }));
  out.push(ev("step_started", { step_id: "update", step_type: "tool", scope }));
  out.push(ev("step_completed", { step_id: "update", scope, output: {} }));
  out.push(ev("loop_iteration_completed", { step_id: "each", scope: [], index, state: null, last_key: `loop:each.${index}:step:update:completed` }));
  return out;
}

describe("deriveRunView with loops", () => {
  it("keeps body instances out of the top-level steps", () => {
    seq = 0;
    const events = [ev("step_started", { step_id: "each", step_type: "foreach" }), ...eachIteration(0, "completed"), ...eachIteration(1, "running")];
    const view = deriveRunView(foreachSpec, events, "running");
    expect(view.steps.each.state).toBe("running");
    expect(view.steps.triage).toBeUndefined();
    expect(Object.keys(view.iterations).sort()).toEqual(["each.0", "each.1"]);
    expect(view.iterations["each.0"].state).toBe("completed");
    expect(view.iterations["each.0"].steps.triage.output).toBe("t0");
    expect(view.iterations["each.1"].state).toBe("running");
    expect(view.iterations["each.1"].steps.triage.state).toBe("running");
    expect(view.iterations["each.1"].steps.update.state).toBe("pending");
    expect(view.iterations["each.1"].steps.triage.childRunId).toBe("child-1");
  });

  it("is the same for out-of-order and duplicated events", () => {
    seq = 0;
    const events = [...eachIteration(0, "completed"), ...eachIteration(1, "failed")];
    const shuffled = [...events].reverse();
    const doubled = [...events, ...events.slice(2, 5)];
    const a = deriveRunView(foreachSpec, events, "running");
    expect(deriveRunView(foreachSpec, shuffled, "running")).toEqual(a);
    expect(deriveRunView(foreachSpec, doubled, "running")).toEqual(a);
    expect(a.iterations["each.1"]).toMatchObject({ state: "failed", errorCode: "model_error", failedStepId: "triage" });
  });

  it("skips the body steps an iteration ended without", () => {
    seq = 0;
    const view = deriveRunView(untilSpec, [
      ev("loop_iteration_started", { step_id: "poll", scope: [], index: 0, state: { n: 0 } }),
      ev("loop_iteration_completed", { step_id: "poll", scope: [], index: 0, state: { n: 1 }, until: "unknown" }),
      ev("loop_iteration_started", { step_id: "poll", scope: [], index: 1, state: { n: 1 } }),
      ev("step_started", { step_id: "check", step_type: "tool", scope: sc(["poll", 1]) }),
    ], "running");
    const first = view.iterations["poll.0"];
    expect(first.steps.check).toMatchObject({ state: "skipped", skipReason: "iteration_ended" });
    expect(first).toMatchObject({ stateIn: { n: 0 }, stateOut: { n: 1 }, until: "unknown" });
  });

  it("nests iterations by scope and creates enclosing ones", () => {
    seq = 0;
    const view = deriveRunView(nestedSpec, [
      ev("step_started", { step_id: "leaf", step_type: "tool", scope: sc(["outer", 2], ["inner", 1]) }),
    ], "running");
    expect(view.iterations["outer.2"]).toMatchObject({ loopId: "outer", index: 2, parentScope: "" });
    expect(view.iterations["outer.2/inner.1"]).toMatchObject({ loopId: "inner", index: 1, parentScope: "outer.2" });
    expect(view.iterations["outer.2/inner.1"].steps.leaf.state).toBe("running");
    expect(view.iterations["outer.2"].startedAt).toBeNull();
  });

  it("interrupts running iterations and body steps when the run is cancelled", () => {
    seq = 0;
    const view = deriveRunView(foreachSpec, [
      ev("step_started", { step_id: "each", step_type: "foreach" }),
      ...eachIteration(0, "running"),
      ev("cancel_requested", { requested_by: "user-1" }),
      ev("run_failed", { error: "the run was cancelled", error_code: "run_cancelled", status: "failed" }),
    ], "running");
    expect(view.cancelRequested).toBe(true);
    expect(view.cancelRequestedBy).toBe("user-1");
    expect(view.cancelled).toBe(true);
    expect(view.status).toBe("failed");
    expect(view.steps.each.state).toBe("failed");
    expect(view.iterations["each.0"].state).toBe("failed");
    expect(view.iterations["each.0"].steps.triage.state).toBe("failed");
  });

  it("does not call an ordinary failure cancelled, and ignores malformed scopes", () => {
    seq = 0;
    const view = deriveRunView(foreachSpec, [
      ev("step_started", { step_id: "triage", scope: [{ loop: "each" }] }),
      ev("run_failed", { error: "x", error_code: "model_error" }),
    ], "running");
    expect(view.cancelled).toBe(false);
    expect(view.cancelRequested).toBe(false);
    expect(view.iterations).toEqual({});
    expect(view.steps.triage).toBeUndefined();
  });
});

describe("resolveRunStates", () => {
  it("shows the latest iteration by default, and the chosen one when picked", () => {
    seq = 0;
    const events = [ev("step_started", { step_id: "each", step_type: "foreach" }), ...eachIteration(0, "completed"), ...eachIteration(1, "failed"), ...eachIteration(2, "running")];
    const view = deriveRunView(foreachSpec, events, "running");
    const latest = resolveRunStates(foreachSpec, view);
    expect(latest.states).toMatchObject({ fetch: "pending", each: "running", triage: "running", update: "pending", report: "pending" });
    expect(latest.pagers).toHaveLength(1);
    expect(latest.pagers[0]).toMatchObject({ loopId: "each", following: true });
    expect(latest.pagers[0].selected?.index).toBe(2);
    expect(latest.badges.each).toBe("×3");
    expect(latest.scopes.triage).toBe("each.2");

    const picked = resolveRunStates(foreachSpec, view, { each: 0 });
    expect(picked.states).toMatchObject({ triage: "completed", update: "completed" });
    expect(picked.details.triage.output).toBe("t0");
    expect(picked.pagers[0].following).toBe(false);
    // A choice that does not exist falls back to the latest.
    expect(resolveRunStates(foreachSpec, view, { each: 9 }).pagers[0]).toMatchObject({ following: true });
  });

  it("leaves a loop's body pending before any iteration, skipped once the loop ended with none", () => {
    seq = 0;
    expect(resolveRunStates(untilSpec, deriveRunView(untilSpec, [], "running")).states.check).toBe("pending");
    const done = deriveRunView(untilSpec, [
      ev("step_started", { step_id: "poll", step_type: "loop" }),
      ev("step_completed", { step_id: "poll", output: { iterations: 0, exited: "condition", last: null, state: {} } }),
      ev("run_completed", { output: null }),
    ], "completed");
    const resolved = resolveRunStates(untilSpec, done);
    expect(resolved.states.check).toBe("skipped");
    expect(resolved.badges.poll).toBe("×0");
  });

  it("resolves nested loops through the outer loop's shown iteration", () => {
    seq = 0;
    const view = deriveRunView(nestedSpec, [
      ev("loop_iteration_started", { step_id: "outer", scope: [], index: 0 }),
      ev("loop_iteration_started", { step_id: "inner", scope: sc(["outer", 0]), index: 0 }),
      ev("step_completed", { step_id: "leaf", scope: sc(["outer", 0], ["inner", 0]), output: 1 }),
      ev("loop_iteration_completed", { step_id: "inner", scope: sc(["outer", 0]), index: 0, state: {} }),
      ev("loop_iteration_started", { step_id: "outer", scope: [], index: 1 }),
      ev("loop_iteration_started", { step_id: "inner", scope: sc(["outer", 1]), index: 0 }),
      ev("loop_iteration_started", { step_id: "inner", scope: sc(["outer", 1]), index: 1 }),
      ev("step_started", { step_id: "leaf", step_type: "tool", scope: sc(["outer", 1], ["inner", 1]) }),
    ], "running");
    const latest = resolveRunStates(nestedSpec, view);
    expect(latest.pagers.map((p) => [p.loopId, p.parentScope, p.iterations.length])).toEqual([["outer", "", 2], ["inner", "outer.1", 2]]);
    expect(latest.states.leaf).toBe("running");
    expect(latest.scopes.leaf).toBe("outer.1/inner.1");
    const first = resolveRunStates(nestedSpec, view, { outer: 0 });
    expect(first.pagers[1]).toMatchObject({ parentScope: "outer.0" });
    expect(first.states.leaf).toBe("completed");
    expect(first.badges).toMatchObject({ outer: "×2", inner: "×1" });
  });

  it("handles 100 iterations", () => {
    seq = 0;
    const events = Array.from({ length: 100 }, (_, i) => eachIteration(i, i === 42 ? "failed" : "completed")).flat();
    const view = deriveRunView(foreachSpec, events, "running");
    const resolved = resolveRunStates(foreachSpec, view);
    expect(resolved.pagers[0].iterations).toHaveLength(100);
    expect(resolved.pagers[0].iterations.map((it) => it.index)).toEqual(Array.from({ length: 100 }, (_, i) => i));
    expect(resolved.badges.each).toBe("×100");
  });
});

describe("pager helpers", () => {
  seq = 0;
  const view = deriveRunView(foreachSpec, [...eachIteration(0, "completed"), ...eachIteration(1, "failed"), ...eachIteration(2, "completed"), ...eachIteration(3, "failed")], "running");
  const its = resolveRunStates(foreachSpec, view).pagers[0].iterations;

  it("steps through every iteration, or the failed ones only", () => {
    expect(adjacentIteration(its, 2, -1)).toBe(1);
    expect(adjacentIteration(its, 2, 1)).toBe(3);
    expect(adjacentIteration(its, 3, 1)).toBeNull();
    expect(adjacentIteration(its, 0, -1)).toBeNull();
    expect(adjacentIteration(its, 2, -1, true)).toBe(1);
    expect(adjacentIteration(its, 1, 1, true)).toBe(3);
    expect(adjacentIteration(its, 0, -1, true)).toBeNull();
    expect(adjacentIteration(its, null, 1, true)).toBe(1);
    expect(adjacentIteration([], null, 1)).toBeNull();
  });

  it("finds body steps and writes scope strings", () => {
    expect(findStep(nestedSpec, "leaf")?.type).toBe("tool");
    expect(findStep(nestedSpec, "missing")).toBeNull();
    expect(scopeString(sc(["outer", 2], ["inner", 0]))).toBe("outer.2/inner.0");
    expect(scopeString([])).toBe("");
  });
});
