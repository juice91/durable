// LP.6 tester acceptance: combinator and loop summaries, badges.
import { describe, expect, it } from "vitest";
import { conditionSummary, loopBadge, loopSummary } from "./workflowGraph";
import type { Expression, ForeachStep, LoopStep } from "./workflows";
import { foreachSpec, refineSpec, untilSpec } from "@/test/workflowLoopFixtures";

const r = (p: string) => ({ $ref: p });
const eq = (p: string, v: unknown) => ({ op: "eq", left: r(p), right: v });
const E = (x: unknown) => x as Expression;

describe("conditionSummary", () => {
  it("leaves a plain comparison as before", () => {
    expect(conditionSummary(E(eq("input.a", 1)))).toBe("input.a eq 1");
    expect(conditionSummary(E({ op: "exists", left: r("input.a") }))).toBe("input.a exists");
  });

  it("renders all, any and not", () => {
    expect(conditionSummary(E({ all: [eq("a", 1), eq("b", 2)] }))).toBe("all(a eq 1, b eq 2)");
    expect(conditionSummary(E({ any: [eq("a", 1), eq("b", "x")] }))).toBe('any(a eq 1, b eq "x")');
    expect(conditionSummary(E({ not: eq("a", true) }))).toBe("not(a eq true)");
  });

  it("renders nested combinators", () => {
    const e = { all: [{ any: [eq("a", 1), { not: eq("b", 2) }] }, { not: { all: [eq("c", 3)] } }] };
    expect(conditionSummary(E(e))).toBe("all(any(a eq 1, not(b eq 2)), not(all(c eq 3)))");
  });

  it("truncates a long combinator at 96 characters with an ellipsis", () => {
    const e = { all: Array.from({ length: 12 }, (_, i) => eq(`input.field_${i}`, i)) };
    const text = conditionSummary(E(e));
    expect(text).toHaveLength(96);
    expect(text.endsWith("…")).toBe(true);
    expect(text.startsWith("all(input.field_0 eq 0, input.field_1 eq 1")).toBe(true);
  });

  it("does not truncate a combinator that fits exactly", () => {
    const short = conditionSummary(E({ any: [eq("a", 1)] }));
    expect(short).toBe("any(a eq 1)");
    expect(short.endsWith("…")).toBe(false);
  });

  it("caps a long single operand at 32 characters", () => {
    const text = conditionSummary(E(eq("x".repeat(60), 1)));
    expect(text.startsWith("x".repeat(31) + "…")).toBe(true);
  });

  it("tolerates malformed expressions without throwing", () => {
    for (const bad of [null, 5, "s", [], {}, { all: "no" }, { not: null }]) {
      expect(() => conditionSummary(bad as never)).not.toThrow();
    }
  });
});

describe("loop summaries and badges", () => {
  const each = foreachSpec.steps[1] as ForeachStep;
  const poll = untilSpec.steps[0] as LoopStep;
  const refine = refineSpec.steps[0] as LoopStep;

  it("foreach: items, bound, concurrency and error rule", () => {
    expect(loopBadge(each)).toBe("×50");
    expect(loopSummary(each)).toEqual(["each of steps.fetch.output.tickets", "max 50 · 4 at a time · stop on error"]);
    expect(loopSummary({ ...each, concurrency: 1, on_item_error: "collect" })[1]).toBe("max 50 · one at a time · collect errors");
    expect(loopSummary({ ...each, concurrency: undefined })[1]).toContain("one at a time");
  });

  it("until loop: condition, bound, fail at max, state", () => {
    expect(loopBadge(poll)).toBe("×10");
    expect(loopSummary(poll)).toEqual(["until steps.check.output.done eq true", "max 10 · fail at max · state"]);
  });

  it("while loop: while condition, exit at max, no state", () => {
    const w = { ...poll, until: undefined, while: E({ not: eq("loop.poll.state.stop", true) }), on_max_iterations: "exit", state: undefined } as unknown as LoopStep;
    expect(loopSummary(w)).toEqual(["while not(loop.poll.state.stop eq true)", "max 10 · exit at max"]);
  });

  it("evaluate-driven loop (refine): until over the evaluate output, exit at max", () => {
    expect(loopSummary(refine)).toEqual(["until steps.judge.output.passed eq true", "max 3 · exit at max · state"]);
    expect(loopBadge(refine)).toBe("×3");
  });

  it("a loop with neither condition says so", () => {
    expect(loopSummary({ ...poll, until: undefined } as unknown as LoopStep)[0]).toBe("no condition");
  });
});
