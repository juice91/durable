// LP.12 acceptance: iteration state from scoped events (flat and nested), bad run ends, malformed scopes.
import { describe, expect, it } from "vitest";
import type { RunEvent } from "./runs";
import { deriveRunView, iterationFailed, loopIterations, resolveRunStates, scopeFromData } from "./workflowRunState";
import { foreachSpec, nestedSpec, untilSpec } from "@/test/workflowLoopFixtures";

let seq = 0;
const ev = (type: string, data: Record<string, unknown>): RunEvent => ({ seq: seq++, type: type as RunEvent["type"], data, ts: "2026-11-15T10:00:00Z" });
const sc = (...frames: [string, number][]) => frames.map(([loop, index]) => ({ loop, index }));

describe("iteration state (flat)", () => {
  it("creates every body step as pending and turns the unrun ones skipped when the iteration ends", () => {
    seq = 0;
    const started = deriveRunView(foreachSpec, [ev("loop_iteration_started", { step_id: "each", scope: [], index: 0 })], "running");
    expect(started.iterations["each.0"].state).toBe("running");
    expect(Object.keys(started.iterations["each.0"].steps).sort()).toEqual(["triage", "update"]);
    expect(started.iterations["each.0"].steps.triage.state).toBe("pending");
    const ended = deriveRunView(foreachSpec, [
      ev("loop_iteration_started", { step_id: "each", scope: [], index: 0 }),
      ev("loop_iteration_completed", { step_id: "each", scope: [], index: 0, state: null }),
    ], "running");
    expect(ended.iterations["each.0"].state).toBe("completed");
    expect(ended.iterations["each.0"].steps.triage).toMatchObject({ state: "skipped", skipReason: "iteration_ended" });
    expect(ended.iterations["each.0"].steps.update.state).toBe("skipped");
  });

  it("records the until verdict and the state in/out", () => {
    seq = 0;
    const view = deriveRunView(untilSpec, [
      ev("loop_iteration_started", { step_id: "poll", scope: [], index: 0, state: { n: 0 } }),
      ev("loop_iteration_completed", { step_id: "poll", scope: [], index: 0, state: { n: 1 }, until: "true" }),
    ], "running");
    expect(view.iterations["poll.0"]).toMatchObject({ state: "completed", until: "true", stateIn: { n: 0 }, stateOut: { n: 1 } });
  });

  it("marks failed iterations with the error, code and failing step", () => {
    seq = 0;
    const view = deriveRunView(foreachSpec, [
      ev("loop_iteration_started", { step_id: "each", scope: [], index: 3 }),
      ev("loop_iteration_failed", { step_id: "each", scope: [], index: 3, status: "failed", error: "bad", error_code: "tool_error", failed_step_id: "update" }),
    ], "running");
    const it = view.iterations["each.3"];
    expect(it).toMatchObject({ state: "failed", error: "bad", errorCode: "tool_error", failedStepId: "update" });
    expect(iterationFailed(it)).toBe(true);
  });

  it("turns running iterations failed on a bad run end, and needs_attention when the run needs attention", () => {
    seq = 0;
    const events = [
      ev("step_started", { step_id: "each", step_type: "foreach" }),
      ev("loop_iteration_started", { step_id: "each", scope: [], index: 0 }),
      ev("step_started", { step_id: "triage", step_type: "agent", scope: sc(["each", 0]) }),
    ];
    const failed = deriveRunView(foreachSpec, [...events, ev("run_failed", { status: "failed", error: "x", error_code: "model_error" })], "running");
    expect(failed.iterations["each.0"].state).toBe("failed");
    expect(failed.iterations["each.0"].steps.triage.state).toBe("failed");
    const attn = deriveRunView(foreachSpec, [...events, ev("run_failed", { status: "needs_attention", error: "x", error_code: "side_effect_unknown" })], "running");
    expect(attn.iterations["each.0"].state).toBe("needs_attention");
    expect(attn.iterations["each.0"].steps.triage.state).toBe("needs_attention");
  });

  it("leaves a completed iteration completed after a later bad run end", () => {
    seq = 0;
    const view = deriveRunView(foreachSpec, [
      ev("loop_iteration_started", { step_id: "each", scope: [], index: 0 }),
      ev("loop_iteration_completed", { step_id: "each", scope: [], index: 0, state: null }),
      ev("run_failed", { status: "failed", error: "x", error_code: "model_error" }),
    ], "running");
    expect(view.iterations["each.0"].state).toBe("completed");
  });
});

describe("iteration state (nested)", () => {
  it("creates ancestor iterations from a deep scoped event and keeps scopes separate", () => {
    seq = 0;
    const view = deriveRunView(nestedSpec, [
      ev("step_started", { step_id: "leaf", step_type: "tool", scope: sc(["outer", 1], ["inner", 2]) }),
      ev("step_started", { step_id: "leaf", step_type: "tool", scope: sc(["outer", 0], ["inner", 0]) }),
    ], "running");
    expect(Object.keys(view.iterations).sort()).toEqual(["outer.0", "outer.0/inner.0", "outer.1", "outer.1/inner.2"]);
    expect(view.iterations["outer.1/inner.2"].parentScope).toBe("outer.1");
    expect(loopIterations(view, "inner", "outer.1").map((it) => it.index)).toEqual([2]);
    expect(loopIterations(view, "inner", "outer.0").map((it) => it.index)).toEqual([0]);
    expect(view.steps.leaf).toBeUndefined();
  });

  it("ends an inner iteration without touching the outer one", () => {
    seq = 0;
    const view = deriveRunView(nestedSpec, [
      ev("loop_iteration_started", { step_id: "outer", scope: [], index: 0 }),
      ev("loop_iteration_started", { step_id: "inner", scope: sc(["outer", 0]), index: 0 }),
      ev("loop_iteration_failed", { step_id: "inner", scope: sc(["outer", 0]), index: 0, status: "failed", error_code: "x" }),
    ], "running");
    expect(view.iterations["outer.0/inner.0"].state).toBe("failed");
    expect(view.iterations["outer.0"].state).toBe("running");
  });

  it("pending outer iteration resolves its inner pager only through the shown outer iteration", () => {
    seq = 0;
    const view = deriveRunView(nestedSpec, [
      ev("loop_iteration_started", { step_id: "outer", scope: [], index: 0 }),
      ev("loop_iteration_started", { step_id: "outer", scope: [], index: 1 }),
    ], "running");
    const r = resolveRunStates(nestedSpec, view, { outer: 0 });
    expect(r.pagers.map((p) => p.loopId)).toEqual(["outer", "inner"]);
    expect(r.pagers[1].iterations).toHaveLength(0);
    expect(r.states.leaf).toBe("pending");
  });
});

describe("malformed scopes", () => {
  const bad: unknown[] = [
    [{ loop: "each" }],
    [{ index: 1 }],
    [{ loop: "each", index: -1 }],
    [{ loop: "each", index: 1.5 }],
    [{ loop: "each", index: "1" }],
    [{ loop: "", index: 1 }],
    [null],
    "each.1",
    { loop: "each", index: 1 },
  ];
  it.each(bad.map((s, i) => [i, s]))("drops an event with malformed scope #%i", (_i, scope) => {
    seq = 0;
    const view = deriveRunView(foreachSpec, [
      ev("step_started", { step_id: "triage", step_type: "agent", scope }),
      ev("loop_iteration_started", { step_id: "each", scope, index: 0 }),
    ], "running");
    expect(view.iterations).toEqual({});
    expect(view.steps.triage).toBeUndefined();
  });

  it("treats an empty or absent scope as top level", () => {
    expect(scopeFromData([])).toEqual([]);
    expect(scopeFromData(undefined)).toBeNull();
    seq = 0;
    const view = deriveRunView(foreachSpec, [ev("step_started", { step_id: "fetch", step_type: "tool" })], "running");
    expect(view.steps.fetch.state).toBe("running");
    expect(view.iterations).toEqual({});
  });
});
