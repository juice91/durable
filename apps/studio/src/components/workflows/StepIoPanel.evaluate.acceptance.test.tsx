// LP.15 acceptance (tester): evaluate results and refine attempts in StepIoPanel: every verdict
// state, order, the nested variant, long and hostile reasons, empty states, keyboard and colours.
import React from "react";
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { IterationDetail, StepDetail } from "@/lib/workflowRunState";
import type { WorkflowStep } from "@/lib/workflows";
import { refineSpec } from "@/test/workflowLoopFixtures";
import { expectNoAxeViolations, AXE_TIMEOUT_MS } from "@/test/a11y";
import { StepIoPanel } from "./StepIoPanel";
import { refineAttempts, refineEvaluateStep } from "./refineAttempts";

afterEach(cleanup);

const loop = refineSpec.steps[0] as WorkflowStep;

const detail = (o: Partial<StepDetail> = {}): StepDetail => ({
  stepId: "judge", state: "completed", stepType: "evaluate", started: true, hasOutput: false, error: null,
  errorCode: null, childRunId: null, agentId: null, toolId: null, prompt: null, deadline: null, timeoutHours: null,
  decision: null, skipReason: null, startedAt: null, endedAt: null, ...o,
});

const iter = (index: number, steps: Record<string, StepDetail>, o: Partial<IterationDetail> = {}, scope = `refine.${index}`): IterationDetail => ({
  loopId: "refine", index, scope, parentScope: "", state: "completed", startedAt: null, endedAt: null,
  error: null, errorCode: null, failedStepId: null, stateIn: {}, until: null, steps, deadEdges: new Set(), ...o,
});

const out = (passed: boolean | null, score: number | null, reason: string | null, extra: object = {}) => ({
  passed, score, reason, results: [{ name: "judge", kind: "llm_judge", passed, score, reason, ...extra }],
});
const writeStep = (draft: unknown) => detail({ stepId: "write", stepType: "agent", hasOutput: true, output: { draft } });
const judgeStep = (output: unknown) => detail({ hasOutput: true, output });

const panel = (iterations: IterationDetail[], step: WorkflowStep | null = loop) =>
  render(<StepIoPanel step={step} detail={detail({ stepId: "refine", stepType: "loop" })} onClose={() => undefined} iterations={iterations} />);

describe("refine attempts: states and order", () => {
  const iterations = [
    iter(3, { write: writeStep("d4"), judge: detail({ state: "failed", errorCode: "evaluate_failed" }) }),
    iter(1, { write: writeStep("d2"), judge: judgeStep(out(null, null, null, { skipped: true })) }),
    iter(2, { write: writeStep("d3"), judge: judgeStep(out(true, 1, "Great.")) }),
    iter(0, { write: writeStep("d1"), judge: judgeStep(out(false, 0.4, "Too short.")) }),
    iter(4, {}, { state: "running" }),
  ];

  it("orders by iteration index and gives pass, fail, skipped, error and pending their own states", () => {
    panel(iterations);
    const items = screen.getAllByTestId("step-refine-attempt");
    expect(items.map((i) => i.getAttribute("data-index"))).toEqual(["0", "1", "2", "3", "4"]);
    expect(items.map((i) => i.getAttribute("data-passed"))).toEqual(["false", "none", "true", "none", "none"]);
    expect(screen.getByTestId("step-refine-attempts")).toHaveTextContent("Attempts (5)");
    expect(items[3]).toHaveTextContent("evaluate_failed");
    expect(within(items[3]).queryByTestId("step-refine-attempt-score")).toBeNull();
    expect(within(items[4]).queryByTestId("step-refine-attempt-draft")).toBeNull();
    expect(within(items[0]).getByTestId("step-refine-attempt-score")).toHaveTextContent("0.40");
    expect(within(items[2]).getByTestId("step-refine-attempt-reason")).toHaveTextContent("Great.");
  });

  it("an object draft is shown as text and a draft ref to the run input shows none", () => {
    panel([iter(0, { write: writeStep({ a: 1, b: ["x"] }), judge: judgeStep(out(true, 1, null)) })]);
    expect(screen.getByTestId("step-refine-attempt-draft").textContent).toMatch(/"a"\s*:\s*1/);
    cleanup();
    const inputRef = { ...loop, body: { ...(loop as { body: object }).body, steps: (loop as unknown as { body: { steps: WorkflowStep[] } }).body.steps.map((s) => (s.type === "evaluate" ? { ...s, value: { $ref: "input.text" } } : s)) } } as WorkflowStep;
    panel([iter(0, { write: writeStep("d"), judge: judgeStep(out(true, 1, null)) })], inputRef);
    expect(screen.getByTestId("step-refine-attempt")).toBeInTheDocument();
    expect(screen.queryByTestId("step-refine-attempt-draft")).toBeNull();
  });
});

describe("nested variant", () => {
  it("a refine loop inside a foreach lists the attempts of its own iterations (nested scope)", () => {
    const iterations = [0, 1].map((i) => iter(i, { write: writeStep(`n${i}`), judge: judgeStep(out(i === 1, i, `r${i}`)) }, { parentScope: "each.2" }, `each.2/refine.${i}`));
    panel(iterations);
    const items = screen.getAllByTestId("step-refine-attempt");
    expect(items).toHaveLength(2);
    expect(within(items[1]).getByTestId("step-refine-attempt-draft")).toHaveTextContent("n1");
  });

  it("an outer loop whose body only holds another loop, and a foreach, have no attempts", () => {
    const outer = { id: "o", type: "loop", max_iterations: 2, body: { start: "refine", steps: [loop] }, next: null } as unknown as WorkflowStep;
    expect(refineEvaluateStep(outer)).toBeNull();
    expect(refineAttempts(outer, [iter(0, {})])).toEqual([]);
    panel([iter(0, {})], outer);
    expect(screen.queryByTestId("step-refine-attempts")).toBeNull();
  });
});

describe("hostile and long reasons", () => {
  const LONG = "x".repeat(20_000);
  const HTML = `<img src=x onerror="window.__pwn=1"><script>window.__pwn=1</script>**bold** [l](javascript:alert(1))`;

  it("renders a very long unbroken reason as wrapping text, inside bounded containers", () => {
    panel([iter(0, { write: writeStep(LONG), judge: judgeStep(out(false, 0, LONG)) })]);
    const reason = screen.getByTestId("step-refine-attempt-reason");
    expect(reason.className).toContain("break-words");
    expect(reason.textContent).toBe(LONG);
    const draft = screen.getByTestId("step-refine-attempt-draft");
    expect(draft.className).toContain("break-words");
    // The draft and the list scroll inside clipped regions a keyboard can reach (review MINOR 6).
    const draftScroll = screen.getByRole("region", { name: "Attempt 1 draft" });
    expect(draftScroll).toContainElement(draft);
    expect(draftScroll.className).toMatch(/max-h-40/);
    expect(draftScroll.className).toMatch(/overflow-auto/);
    expect(draftScroll).toHaveAttribute("tabindex", "0");
    const listScroll = screen.getByRole("region", { name: "Refine attempts" });
    expect(listScroll).toContainElement(screen.getByRole("list", { name: "Refine attempts" }));
    expect(listScroll.className).toMatch(/max-h-96/);
    expect(listScroll).toHaveAttribute("tabindex", "0");
  });

  it("clips the step reason and each evaluator reason in height (review MINOR 2)", () => {
    const results = Array.from({ length: 8 }, (_, i) => ({ name: `judge${i}`, kind: "llm_judge", passed: false, score: 0, reason: "r".repeat(2000) }));
    render(<StepIoPanel step={null} detail={detail({ hasOutput: true, output: { passed: false, score: 0, reason: "s".repeat(2000), results } })} onClose={() => undefined} />);
    const stepReason = screen.getByRole("region", { name: "Evaluation reason" });
    expect(stepReason).toContainElement(screen.getByTestId("step-io-evaluation-reason"));
    expect(stepReason.className).toMatch(/max-h-40/);
    expect(stepReason.className).toMatch(/overflow-auto/);
    expect(stepReason).toHaveAttribute("tabindex", "0");
    const reasons = screen.getAllByTestId("step-io-evaluator-reason");
    expect(reasons).toHaveLength(8);
    reasons.forEach((reason, i) => {
      const region = screen.getByRole("region", { name: `Reason of evaluator ${i + 1} (judge${i})` });
      expect(region).toContainElement(reason);
      expect(region.className).toMatch(/max-h-24/);
      expect(region.className).toMatch(/overflow-auto/);
      expect(region).toHaveAttribute("tabindex", "0");
      expect(reason.textContent).toBe("r".repeat(2000));
    });
  });

  it("the clipped reason regions have no axe violations", async () => {
    const { container } = render(<StepIoPanel step={null} detail={detail({ hasOutput: true, output: { passed: false, score: 0.5, reason: "why", results: [{ name: "a", kind: "contains", passed: false, score: 0, reason: "a reason" }, { name: "a", kind: "contains", passed: true, score: 1, reason: "same name" }] } })} onClose={() => undefined} />);
    await expectNoAxeViolations(container);
  }, AXE_TIMEOUT_MS);

  it("shows HTML and Markdown in a reason as plain text; nothing is created or run", () => {
    render(<StepIoPanel step={null} detail={detail({ hasOutput: true, output: out(false, 0, HTML) })} onClose={() => undefined} />);
    expect(screen.getByTestId("step-io-evaluation-reason").textContent).toBe(HTML);
    const root = screen.getByTestId("step-io-evaluation");
    expect(root.querySelector("img, script, a, strong")).toBeNull();
    expect((window as unknown as { __pwn?: number }).__pwn).toBeUndefined();
  });

  it("a long evaluator name and reason wrap in the evaluator list", () => {
    render(<StepIoPanel step={null} detail={detail({ hasOutput: true, output: { passed: false, score: 0, reason: null, results: [{ name: LONG, kind: "contains", passed: false, score: 0, reason: LONG }] } })} onClose={() => undefined} />);
    const row = screen.getByTestId("step-io-evaluator-result");
    expect(row.className).toContain("min-w-0");
    expect(row.innerHTML).toContain("break-words");
  });
});

describe("empty states", () => {
  it("an evaluate step with no output yet, a pending one and one with no results", () => {
    render(<StepIoPanel step={null} detail={detail({ state: "running", hasOutput: false })} onClose={() => undefined} />);
    expect(screen.queryByTestId("step-io-evaluation")).toBeNull();
    cleanup();
    render(<StepIoPanel step={null} detail={detail({ hasOutput: true, output: { passed: true, score: 1, reason: null, results: [] } })} onClose={() => undefined} />);
    expect(screen.getByTestId("step-io-evaluation-verdict")).toHaveAttribute("data-passed", "true");
    expect(screen.queryByTestId("step-io-evaluator-result")).toBeNull();
    expect(screen.queryByTestId("step-io-evaluation-reason")).toBeNull();
  });

  it("an unknown verdict shows a dash score and no verdict badge claim", () => {
    render(<StepIoPanel step={null} detail={detail({ hasOutput: true, output: { results: [{ name: "a" }] } })} onClose={() => undefined} />);
    expect(screen.getByTestId("step-io-evaluation-score")).toHaveTextContent("-");
    expect(screen.getByTestId("step-io-evaluation-verdict")).toHaveAttribute("data-passed", "none");
  });

  it("a loop with no iterations, none given, or a non-refine step shows no attempts", () => {
    panel([]);
    expect(screen.queryByTestId("step-refine-attempts")).toBeNull();
    cleanup();
    render(<StepIoPanel step={loop} detail={detail({ stepId: "refine", stepType: "loop" })} onClose={() => undefined} iterations={null} />);
    expect(screen.queryByTestId("step-refine-attempts")).toBeNull();
    cleanup();
    render(<StepIoPanel step={null} detail={detail({ stepId: "t", stepType: "tool" })} onClose={() => undefined} iterations={[iter(0, {})]} />);
    expect(screen.queryByTestId("step-refine-attempts")).toBeNull();
  });
});

describe("keyboard, axe and tokens", () => {
  it("Close and the raw-output disclosure are reachable and operable by keyboard", () => {
    const onClose = vi.fn();
    render(<StepIoPanel step={null} detail={detail({ hasOutput: true, output: out(true, 1, "ok") })} onClose={onClose} />);
    const close = screen.getByRole("button", { name: "Close step details" });
    close.focus();
    expect(document.activeElement).toBe(close);
    fireEvent.click(close);
    expect(onClose).toHaveBeenCalled();
    const summary = screen.getByText(/raw output/i).closest("summary");
    expect(summary).not.toBeNull();
    expect((summary as HTMLElement).tabIndex).toBeGreaterThanOrEqual(0);
  });

  it("has no axe violations for every verdict state", async () => {
    panel([
      iter(0, { write: writeStep("d"), judge: judgeStep(out(false, 0.1, "no")) }),
      iter(1, { write: writeStep("d"), judge: judgeStep(out(null, null, null, { skipped: true, error_code: "x" })) }),
      iter(2, { write: writeStep("d"), judge: detail({ state: "failed", errorCode: "evaluate_failed" }) }),
    ]);
    await expectNoAxeViolations(document.body);
  }, AXE_TIMEOUT_MS);

  it("the LP.15 sources use design tokens only: no raw colours, palette classes or inline styles", () => {
    const base = join(__dirname, "..");
    for (const f of ["workflows/StepIoPanel.tsx", "workflows/refineAttempts.ts", "traces/LoopSpanSections.tsx"]) {
      const text = readFileSync(join(base, f), "utf8");
      expect(text, f).not.toMatch(/#[0-9a-fA-F]{3,8}\b/);
      expect(text, f).not.toMatch(/\b(?:rgb|rgba|hsl|hsla|oklch)\(/);
      expect(text, f).not.toMatch(/\b(?:bg|text|border|ring|fill|stroke|from|to|via)-(?:red|green|blue|yellow|amber|orange|emerald|rose|sky|slate|gray|zinc|neutral|stone|lime|teal|cyan|indigo|violet|purple|pink|fuchsia|white|black)(?:-\d{2,3})?\b/);
      expect(text, f).not.toMatch(/style=\{\{[^}]*(?:color|background)/);
    }
  });
});
