// LP.9: the loop span kinds are declared, labelled and styled.
import { describe, expect, it } from "vitest";
import { KIND_BADGE, KIND_ICON, KIND_TONE } from "@/components/traces/spanStyles";
import { KIND_LABELS, SPAN_KINDS, flattenSpans, spanLabel, type TraceSpan } from "./traces";

function span(id: string, parent: string | null, kind: TraceSpan["kind"], attributes: Record<string, unknown>): TraceSpan {
  return {
    id, parent_id: parent, kind, name: id, start: null, end: null, status: "ok",
    attributes, input: null, output: null, truncated: {},
  };
}

describe("loop span kinds", () => {
  it("every kind has a label, badge, icon and tone", () => {
    for (const kind of ["loop", "loop_iteration"] as const) {
      expect(SPAN_KINDS).toContain(kind);
      expect(KIND_LABELS[kind]).toBeTruthy();
      expect(KIND_BADGE[kind]).toBeTruthy();
      expect(KIND_ICON[kind]).toBeTruthy();
      expect(KIND_TONE[kind]).toBeTruthy();
    }
  });

  it("labels a loop and its iterations (1-based)", () => {
    const loop = span("r:step:refine", "run:r", "loop", { step_type: "loop", iterations_completed: 3 });
    loop.name = "refine";
    expect(spanLabel(loop)).toBe("Loop: refine (loop), 3 done");
    const it2 = span("r:loop:refine.1", loop.id, "loop_iteration", { step_id: "refine", index: 1 });
    expect(spanLabel(it2)).toBe("Iteration 2: refine");
  });

  it("nests body spans under their iteration", () => {
    const spans = [
      span("run:r", null, "run", {}),
      span("r:step:each", "run:r", "loop", {}),
      span("r:loop:each.0", "r:step:each", "loop_iteration", { index: 0 }),
      span("r:step:each.0~triage", "r:loop:each.0", "workflow_step", {}),
    ];
    const rows = flattenSpans(spans);
    expect(rows.map((r) => r.span.id)).toEqual(spans.map((s) => s.id));
    expect(rows.map((r) => r.depth)).toEqual([0, 1, 2, 3]);
  });
});
