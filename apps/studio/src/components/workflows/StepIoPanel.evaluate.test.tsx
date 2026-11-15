// LP.15: the evaluate step's results and a refine loop's attempts (draft, score, reason) in the
// run view's step detail; the pure helpers behind them.
import React from "react";
import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import type { RunEvent, SubscribeHandlers } from "@/lib/runs";
import type { IterationDetail, StepDetail } from "@/lib/workflowRunState";
import { refineSpec } from "@/test/workflowLoopFixtures";
import { expectNoAxeViolations, AXE_TIMEOUT_MS } from "@/test/a11y";
import { setWindowWidth } from "@/components/frame/frameTesting";

const runsMock = vi.hoisted(() => ({ getRun: vi.fn(), listEvents: vi.fn(), subscribeRun: vi.fn() }));
const wfMock = vi.hoisted(() => ({ getWorkflowVersion: vi.fn(), decideApproval: vi.fn(), cancelRun: vi.fn() }));

vi.mock("next/link", () => ({ default: ({ href, children, ...rest }: { href: string; children: React.ReactNode }) => <a href={href} {...rest}>{children}</a> }));
vi.mock("@/lib/runs", async (importOriginal) => ({ ...(await importOriginal<object>()), ...runsMock }));
vi.mock("@/lib/workflows", async (importOriginal) => ({ ...(await importOriginal<object>()), ...wfMock }));

import { WorkflowRunView } from "./WorkflowRunView";
import { StepIoPanel } from "./StepIoPanel";
import { evaluationView, readInIteration, refineAttempts, refineEvaluateStep } from "./refineAttempts";

let handlers: SubscribeHandlers;
let seq = 0;
const ev = (type: string, data: Record<string, unknown> = {}, ts = "2026-11-15T10:00:00Z"): RunEvent => ({ seq: seq++, type: type as RunEvent["type"], data, ts });
const emit = (...events: RunEvent[]) => act(() => events.forEach((event) => handlers.onEvent?.(event)));

const RUN = {
  run_id: "run-1", kind: "workflow", workflow_id: "wf-1", workflow_version: 1, agent_id: null, version: null,
  status: "running", display_status: "running", input: "{}", output: null, error: null, heartbeat_at: null,
  created_at: "2026-11-15T10:00:00Z", updated_at: "2026-11-15T10:00:05Z", trace_id: null, conversation_id: null, turn: null,
};

const REASON = (n: number) => `The answer does not contain the required marker "attempt 3" (attempt ${n}).`;

function judgeOutput(n: number) {
  const passed = n === 3;
  return {
    passed,
    score: passed ? 1 : 0.25,
    reason: passed ? "Contains the marker." : REASON(n),
    results: [{ name: "llm_judge", kind: "llm_judge", passed, score: passed ? 1 : 0.25, reason: passed ? "Contains the marker." : REASON(n) }],
  };
}

function attempt(index: number): RunEvent[] {
  const scope = [{ loop: "refine", index }];
  const n = index + 1;
  return [
    ev("loop_iteration_started", { step_id: "refine", scope: [], index, state: { feedback: index ? REASON(index) : "" } }),
    ev("step_started", { step_id: "write", step_type: "agent", scope }),
    ev("step_completed", { step_id: "write", scope, output: { draft: `attempt ${n}` } }),
    ev("step_started", { step_id: "judge", step_type: "evaluate", scope }),
    ev("step_completed", { step_id: "judge", scope, output: judgeOutput(n) }),
    ev("loop_iteration_completed", { step_id: "refine", scope: [], index, state: { feedback: judgeOutput(n).reason }, until: n === 3 ? "true" : "false", last_key: `loop:refine.${index}:step:judge:completed` }),
  ];
}

const refineRun = () => [ev("step_started", { step_id: "refine", step_type: "loop" }), ...attempt(0), ...attempt(1), ...attempt(2)];

let restoreWidth: () => void = () => undefined;

beforeAll(() => {
  class RO { observe() {} unobserve() {} disconnect() {} }
  vi.stubGlobal("ResizeObserver", RO);
  vi.stubGlobal("DOMMatrixReadOnly", class { m22 = 1; });
});

beforeEach(() => {
  seq = 0;
  localStorage.clear();
  runsMock.getRun.mockResolvedValue(RUN);
  runsMock.listEvents.mockResolvedValue([]);
  runsMock.subscribeRun.mockImplementation((_id: string, h: SubscribeHandlers) => {
    handlers = h;
    return () => undefined;
  });
  wfMock.getWorkflowVersion.mockResolvedValue({ workflow_id: "wf-1", version: 1, spec: refineSpec, spec_hash: "h", created_at: "", created_by: "u" });
});

afterEach(() => {
  cleanup();
  restoreWidth();
  restoreWidth = () => undefined;
});

async function openStep(id: string) {
  render(<WorkflowRunView runId="run-1" workflowId="wf-1" />);
  await screen.findByTestId("workflow-step-write");
  emit(...refineRun());
  fireEvent.click(within(screen.getByTestId(`workflow-step-${id}`)).getAllByRole("button")[0]);
  return screen.findByTestId("step-io-panel");
}

describe("refine attempts in the loop step detail", () => {
  it("lists each attempt's draft, score and reason; the third passes", async () => {
    const panel = await openStep("refine");
    const list = within(panel).getByTestId("step-refine-attempts");
    expect(list).toHaveTextContent("Attempts (3)");
    expect(list).toHaveTextContent("Judged by judge.");
    const items = within(list).getAllByTestId("step-refine-attempt");
    expect(items.map((i) => i.getAttribute("data-index"))).toEqual(["0", "1", "2"]);
    expect(items.map((i) => i.getAttribute("data-passed"))).toEqual(["false", "false", "true"]);
    expect(items.map((i) => within(i).getByTestId("step-refine-attempt-draft").textContent)).toEqual(["attempt 1", "attempt 2", "attempt 3"]);
    expect(items.map((i) => within(i).getByTestId("step-refine-attempt-score").textContent)).toEqual(["0.25", "0.25", "1.00"]);
    expect(within(items[0]).getByTestId("step-refine-attempt-reason")).toHaveTextContent(REASON(1));
    expect(within(items[2]).getByText("Passed")).toBeInTheDocument();
    // The LP.12 iteration table is still there.
    expect(within(panel).getByTestId("workflow-loop-iterations-refine")).toBeInTheDocument();
  });

  it("shows the selected iteration's evaluate results in the evaluate step's detail", async () => {
    const panel = await openStep("judge");
    expect(within(panel).getByTestId("step-io-iteration")).toHaveTextContent("From");
    expect(within(panel).getByTestId("step-io-evaluation-verdict")).toHaveAttribute("data-passed", "true");
    expect(within(panel).getByTestId("step-io-evaluation-score")).toHaveTextContent("1.00");
    expect(within(panel).getByTestId("step-io-evaluation-reason")).toHaveTextContent("Contains the marker.");
    expect(within(panel).getAllByTestId("step-io-evaluator-result")).toHaveLength(1);
    expect(within(panel).queryByTestId("step-refine-attempts")).not.toBeInTheDocument();
  });

  it("fits at 375 px", async () => {
    restoreWidth = setWindowWidth(375);
    render(<WorkflowRunView runId="run-1" workflowId="wf-1" />);
    await screen.findByTestId("workflow-graph");
    emit(...refineRun());
    // Narrow: the step list is in the bottom Sheet; a step opens its detail as a Sheet.
    fireEvent.click(screen.getByRole("button", { name: /details/i }));
    const row = await screen.findByTestId("workflow-step-refine");
    fireEvent.click(within(row).getAllByRole("button")[0]);
    const list = await screen.findByTestId("step-refine-attempts");
    expect(screen.getByTestId("workflow-step-sheet")).toContainElement(list);
    expect(within(list).getAllByTestId("step-refine-attempt")).toHaveLength(3);
    for (const item of within(list).getAllByTestId("step-refine-attempt")) {
      expect(item.className).toContain("min-w-0");
      expect(within(item).getByTestId("step-refine-attempt-draft").className).toContain("break-words");
    }
  });

  it("has no axe violations with the refine attempts", async () => {
    await openStep("refine");
    await screen.findByTestId("step-refine-attempts");
    await expectNoAxeViolations(document.body);
  }, AXE_TIMEOUT_MS);

  it("has no axe violations with the evaluation", async () => {
    await openStep("judge");
    await screen.findByTestId("step-io-evaluation");
    await expectNoAxeViolations(document.body);
  }, AXE_TIMEOUT_MS);
});

// ------------------------------------------------------------------------------------ unit

const detail = (overrides: Partial<StepDetail>): StepDetail => ({
  stepId: "judge", state: "completed", stepType: "evaluate", started: true, hasOutput: false, error: null,
  errorCode: null, childRunId: null, agentId: null, toolId: null, prompt: null, deadline: null, timeoutHours: null,
  decision: null, skipReason: null, startedAt: null, endedAt: null, ...overrides,
});

const iterationOf = (index: number, steps: Record<string, StepDetail>, stateIn: unknown = {}): IterationDetail => ({
  loopId: "refine", index, scope: `refine.${index}`, parentScope: "", state: "completed", startedAt: null, endedAt: null,
  error: null, errorCode: null, failedStepId: null, stateIn, until: null, steps, deadEdges: new Set(),
});

describe("evaluate step detail", () => {
  it("renders failed, skipped and errored evaluators, with the raw output behind a disclosure", () => {
    const output = {
      passed: false, score: 0.5, reason: "line one\nline two",
      results: [
        { name: "contains", kind: "contains", passed: true, score: 1, reason: "ok" },
        { name: "judge", kind: "llm_judge", passed: false, score: null, reason: "the judge request failed", error_type: "JudgeError", error_code: "judge_failed" },
        { name: "equals", kind: "equals", passed: null, score: null, reason: null, skipped: true },
      ],
    };
    render(<StepIoPanel step={null} detail={detail({ hasOutput: true, output })} onClose={() => undefined} />);
    expect(screen.getByTestId("step-io-evaluation-verdict")).toHaveTextContent("Failed");
    expect(screen.getByTestId("step-io-evaluation-reason").textContent).toBe("line one\nline two");
    const rows = screen.getAllByTestId("step-io-evaluator-result");
    expect(rows[0]).toHaveTextContent("Passed, 1.00");
    expect(rows[1]).toHaveTextContent("Error (judge_failed)");
    expect(rows[2]).toHaveTextContent("Skipped");
    expect(rows[2]).toHaveAttribute("data-skipped", "true");
    expect(screen.getByTestId("step-io-output").closest("details")).not.toBeNull();
  });

  it("falls back to the plain output block for an unexpected shape", () => {
    render(<StepIoPanel step={null} detail={detail({ hasOutput: true, output: "text" })} onClose={() => undefined} />);
    expect(screen.queryByTestId("step-io-evaluation")).not.toBeInTheDocument();
    expect(screen.getByTestId("step-io-output")).toHaveTextContent("text");
  });
});

describe("refineAttempts", () => {
  const loop = refineSpec.steps[0];

  it("finds the evaluate step the until reads, else the first", () => {
    expect(refineEvaluateStep(loop)?.id).toBe("judge");
    expect(refineEvaluateStep(null)).toBeNull();
    expect(refineEvaluateStep({ ...loop, type: "foreach" } as never)).toBeNull();
    const twoEvals = {
      ...loop,
      until: { op: "eq", left: { $ref: "steps.second.output.passed" }, right: true },
      body: { start: "write", steps: [
        ...(loop.type === "loop" ? loop.body.steps : []),
        { id: "second", type: "evaluate", value: { $ref: "loop.refine.state.feedback" }, evaluators: [{ kind: "contains", value: "x" }], next: null },
      ] },
    } as typeof loop;
    expect(refineEvaluateStep(twoEvals)?.id).toBe("second");
  });

  it("reads a step output or the loop state, never anything else", () => {
    const it0 = iterationOf(0, { write: detail({ stepId: "write", hasOutput: true, output: { draft: "d", list: ["a", "b"] } }) }, { feedback: "fb" });
    expect(readInIteration("steps.write.output.draft", it0, "refine")).toBe("d");
    expect(readInIteration("steps.write.output.list.1", it0, "refine")).toBe("b");
    expect(readInIteration("steps.write.output.nope", it0, "refine")).toBeUndefined();
    expect(readInIteration("steps.other.output", it0, "refine")).toBeUndefined();
    expect(readInIteration("loop.refine.state.feedback", it0, "refine")).toBe("fb");
    expect(readInIteration("loop.outer.state.feedback", it0, "refine")).toBeUndefined();
    expect(readInIteration("input.text", it0, "refine")).toBeUndefined();
  });

  it("gives one attempt per iteration in index order, open and failed ones included", () => {
    const iterations = [
      iterationOf(1, { write: detail({ stepId: "write", hasOutput: true, output: { draft: "two" } }), judge: detail({ state: "failed", errorCode: "run_budget_exceeded" }) }),
      iterationOf(0, { write: detail({ stepId: "write", hasOutput: true, output: { draft: "one" } }), judge: detail({ hasOutput: true, output: judgeOutput(1) }) }),
      // An open iteration: its write step is still running and its evaluate step has not started.
      { ...iterationOf(2, { write: detail({ stepId: "write", state: "running" }) }), state: "running" as const },
    ];
    const attempts = refineAttempts(loop, iterations);
    expect(attempts.map((a) => a.index)).toEqual([0, 1, 2]);
    expect(attempts[0]).toMatchObject({ draft: "one", errorCode: null });
    expect(attempts[0].evaluation?.score).toBe(0.25);
    expect(attempts[1]).toMatchObject({ draft: "two", evaluation: null, errorCode: "run_budget_exceeded" });
    expect(attempts[2]).toMatchObject({ iterationState: "running", draft: undefined, evaluation: null, errorCode: null });
    expect(refineAttempts(loop, null)).toEqual([]);
  });

  it("evaluationView rejects non-evaluate shapes", () => {
    expect(evaluationView(null)).toBeNull();
    expect(evaluationView([1])).toBeNull();
    expect(evaluationView({ a: 1 })).toBeNull();
    expect(evaluationView({ passed: true })).toMatchObject({ passed: true, score: null, reason: null, results: [] });
  });
});
