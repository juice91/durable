import { describe, expect, it } from "vitest";
import type { RunEvent } from "./runs";
import { deriveRunView, mergeEvents } from "./workflowRunState";
import { demoSpec, parallelSpec } from "@/test/workflowFixtures";

let seq = 0;
function ev(type: RunEvent["type"], data: Record<string, unknown> = {}, at?: number): RunEvent {
  const s = at ?? seq++;
  return { seq: s, type, data, ts: `2026-11-13T10:00:${String(s).padStart(2, "0")}Z` };
}

function states(view: ReturnType<typeof deriveRunView>) {
  return Object.fromEntries(Object.entries(view.steps).map(([id, d]) => [id, d.state]));
}

describe("mergeEvents", () => {
  it("dedupes by seq, sorts out-of-order arrivals and keeps identity when nothing is new", () => {
    const a = ev("run_started", {}, 0);
    const b = ev("step_started", { step_id: "classify" }, 1);
    const c = ev("step_completed", { step_id: "classify" }, 2);
    const merged = mergeEvents([c], [a, c, b, b]);
    expect(merged.map((e) => e.seq)).toEqual([0, 1, 2]);
    expect(mergeEvents(merged, [a, b])).toBe(merged);
  });
});

describe("deriveRunView", () => {
  it("is pending everywhere before any step event", () => {
    const view = deriveRunView(demoSpec, [], "queued");
    expect(new Set(Object.values(states(view)))).toEqual(new Set(["pending"]));
    expect(view.waiting).toEqual([]);
  });

  it("tracks running, waiting with prompt and deadline, and the child run", () => {
    seq = 0;
    const events = [
      ev("run_started"),
      ev("step_started", { step_id: "classify", step_type: "agent", input: { text: "hi" }, child_run_id: "child-1" }),
      ev("step_completed", { step_id: "classify", output: "billing" }),
      ev("step_started", { step_id: "lookup", step_type: "tool", tool_id: "http.get", input: {} }),
      ev("step_completed", { step_id: "lookup", output: { ok: true } }),
      ev("step_started", { step_id: "route", step_type: "branch" }),
      ev("step_completed", { step_id: "route", output: { case: 1, next: "approve" } }),
      ev("step_started", { step_id: "approve", step_type: "approval" }),
      ev("step_waiting", { step_id: "approve", prompt: "Escalate?", timeout_hours: 4, deadline: "2026-11-13T14:00:00Z" }),
    ];
    const view = deriveRunView(demoSpec, events, "running");
    expect(states(view)).toEqual({ classify: "completed", lookup: "completed", route: "completed", approve: "waiting", summarize: "pending" });
    expect(view.steps.classify.childRunId).toBe("child-1");
    expect(view.steps.classify.input).toEqual({ text: "hi" });
    expect(view.steps.lookup.output).toEqual({ ok: true });
    expect(view.waiting).toEqual(["approve"]);
    expect(view.steps.approve).toMatchObject({ prompt: "Escalate?", deadline: "2026-11-13T14:00:00Z" });
  });

  it("skips the branch arm not taken as soon as the branch decides", () => {
    const spec = { ...demoSpec, steps: demoSpec.steps.map((s) => (s.id === "approve" ? { ...s, next: null } : s)) };
    seq = 0;
    const events = [
      ev("step_started", { step_id: "route", step_type: "branch" }),
      ev("step_completed", { step_id: "route", output: { case: null, next: "summarize" } }),
    ];
    const view = deriveRunView(spec, events, "running");
    expect(view.steps.approve.state).toBe("skipped");
    expect(view.steps.summarize.state).toBe("pending");
    expect([...view.deadEdges]).toEqual(["route->approve:case:0"]);
  });

  it("records the decision and shows decided_by and comment", () => {
    seq = 0;
    const events = [
      ev("step_waiting", { step_id: "approve", prompt: "Escalate?" }),
      ev("step_approved", { step_id: "approve", decided_by: "dev-user", comment: "go" }),
      ev("step_completed", { step_id: "approve", output: { approved: true, decided_by: "dev-user", comment: "go" } }),
    ];
    const view = deriveRunView(demoSpec, events, "running");
    expect(view.steps.approve.state).toBe("completed");
    expect(view.steps.approve.decision).toEqual({ kind: "approved", decidedBy: "dev-user", comment: "go" });
    expect(view.waiting).toEqual([]);
  });

  it("fails the step and the run on a rejection, with the error code", () => {
    seq = 0;
    const events = [
      ev("step_waiting", { step_id: "approve", prompt: "?" }),
      ev("step_rejected", { step_id: "approve", decided_by: "u", comment: null }),
      ev("step_failed", { step_id: "approve", status: "failed", error: "rejected", error_code: "approval_rejected" }),
      ev("run_failed", { status: "failed", error: "rejected", error_code: "approval_rejected", step_id: "approve" }),
    ];
    const view = deriveRunView(demoSpec, events, "running");
    expect(view.status).toBe("failed");
    expect(view.steps.approve).toMatchObject({ state: "failed", errorCode: "approval_rejected" });
    expect(view.steps.approve.decision?.kind).toBe("rejected");
    expect(view.runError).toMatchObject({ errorCode: "approval_rejected", stepId: "approve" });
    expect(view.steps.summarize.state).toBe("pending");
  });

  it("marks needs_attention from step_failed and from a run_failed naming a running step", () => {
    seq = 0;
    const view = deriveRunView(demoSpec, [
      ev("step_started", { step_id: "lookup", step_type: "tool" }),
      ev("run_failed", { status: "needs_attention", error: "unknown outcome", error_code: "side_effect_outcome_unknown", step_id: "lookup" }),
    ], "running");
    expect(view.status).toBe("needs_attention");
    expect(view.steps.lookup).toMatchObject({ state: "needs_attention", errorCode: "side_effect_outcome_unknown" });
  });

  it("shows the terminal status of a run that ended with no step events", () => {
    const failed = deriveRunView(demoSpec, [ev("run_failed", { status: "failed", error: "spec invalid", error_code: "workflow_spec_invalid" }, 0)], "running");
    expect(failed.status).toBe("failed");
    expect(failed.runError?.errorCode).toBe("workflow_spec_invalid");
    expect(deriveRunView(demoSpec, [], "completed").status).toBe("completed");
  });

  it("skips what never ran once the run completes, and gives the run output", () => {
    seq = 0;
    const view = deriveRunView(demoSpec, [
      ev("step_completed", { step_id: "route", output: { next: "summarize" } }),
      ev("step_completed", { step_id: "summarize", output: "done" }),
      ev("run_completed", { output: "done", result: "done" }),
    ], "running");
    expect(view.status).toBe("completed");
    expect(view.steps.approve.state).toBe("skipped");
    expect(view.output).toBe("done");
  });

  it("never skips a parallel join because one branch ends early", () => {
    seq = 0;
    const view = deriveRunView(parallelSpec, [
      ev("step_started", { step_id: "fork", step_type: "parallel" }),
      ev("step_completed", { step_id: "b", output: 1 }),
    ], "running");
    expect(view.steps.merge.state).toBe("pending");
    expect(view.steps.a.state).toBe("pending");
  });

  it("is order-independent: the same events shuffled give the same states", () => {
    seq = 0;
    const events = [
      ev("step_started", { step_id: "classify", step_type: "agent" }),
      ev("step_completed", { step_id: "classify", output: "x" }),
      ev("step_started", { step_id: "lookup", step_type: "tool" }),
      ev("step_failed", { step_id: "lookup", status: "failed", error: "boom", error_code: "tool_error" }),
    ];
    const a = deriveRunView(demoSpec, events, "running");
    const b = deriveRunView(demoSpec, [...events].reverse().concat(events), "running");
    expect(states(a)).toEqual(states(b));
    expect(a.steps.lookup).toMatchObject({ state: "failed", error: "boom", errorCode: "tool_error" });
  });
});
