// LP.9 tester: loop span kinds in every kind map, labels with missing attributes.
import { describe, expect, it } from "vitest";
import { KIND_BADGE, KIND_ICON, KIND_TONE } from "@/components/traces/spanStyles";
import { KIND_LABELS, SPAN_KINDS, spanLabel, type TraceSpan } from "./traces";

function span(kind: TraceSpan["kind"], name: string, attributes: Record<string, unknown>): TraceSpan {
  return { id: "x", parent_id: null, kind, name, start: null, end: null, status: "ok", attributes, input: null, output: null, truncated: {} };
}

describe("span kind maps", () => {
  it("have a defined entry for every kind, none undefined", () => {
    for (const kind of SPAN_KINDS) {
      expect(KIND_LABELS[kind], `label ${kind}`).toBeTruthy();
      expect(KIND_BADGE[kind], `badge ${kind}`).toBeTruthy();
      expect(KIND_ICON[kind], `icon ${kind}`).toBeTruthy();
      expect(KIND_TONE[kind], `tone ${kind}`).toBeTruthy();
    }
    for (const map of [KIND_LABELS, KIND_BADGE, KIND_ICON, KIND_TONE]) {
      expect(Object.keys(map).sort()).toEqual([...SPAN_KINDS].sort());
      expect(Object.values(map).every((v) => v !== undefined)).toBe(true);
    }
  });

  it("loop kinds: labels, badges and distinct icons", () => {
    expect(KIND_LABELS.loop).toBe("Loop");
    expect(KIND_LABELS.loop_iteration).toBe("Loop iteration");
    expect(KIND_BADGE.loop).toBe("LOOP");
    expect(KIND_BADGE.loop_iteration).toBe("ITER");
    expect(KIND_ICON.loop).not.toBe(KIND_ICON.loop_iteration);
    expect(KIND_ICON.loop).not.toBe(KIND_ICON.workflow_step);
  });
});

describe("spanLabel for loops", () => {
  it("falls back when attributes are missing", () => {
    expect(spanLabel(span("loop", "each", {}))).toBe("Loop: each");
    expect(spanLabel(span("loop_iteration", "each #4", {}))).toBe("Iteration ?: each #4");
  });
  it("labels a foreach with zero done and index 0 as Iteration 1", () => {
    expect(spanLabel(span("loop", "each", { step_type: "foreach", iterations_completed: 0 }))).toBe("Loop: each (foreach), 0 done");
    expect(spanLabel(span("loop_iteration", "n", { index: 0, step_id: "each" }))).toBe("Iteration 1: each");
  });
});
