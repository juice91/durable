import React from "react";
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { BranchCases, branchTaken } from "./BranchCases";
import { NODE_STATE_LABELS, WorkflowStepList, notReachedHint } from "./WorkflowGraph";
import { NOT_REACHED_OPEN_KEY } from "@/lib/notReached";
import type { BranchStep, WorkflowSpec } from "@/lib/workflows";
import type { NodeState, StepDetail } from "@/lib/workflowRunState";

vi.mock("next/link", () => ({ default: ({ href, children }: { href: string; children: React.ReactNode }) => <a href={href}>{children}</a> }));

const ref = (path: string) => ({ $ref: path });
const step: BranchStep = {
  id: "feed_gate",
  type: "branch",
  cases: [
    { when: { op: "eq", left: ref("steps.feed.output.ok"), right: false }, next: "r_feed_failed" },
    { when: { all: [{ op: "eq", left: ref("input.dry"), right: true }, { op: "eq", left: ref("input.ignore_posted"), right: true }] }, next: "ltn" },
    { when: { any: [{ op: "contains", left: ref("steps.clock.output"), right: "T00:" }, { all: [{ op: "exists", left: ref("input.x") }] }] }, next: "r_slot_done" },
  ],
  default: "ltn",
};

beforeEach(() => window.localStorage.clear());
afterEach(() => cleanup());

describe("BranchCases", () => {
  it("lists numbered cases, all/any as a sub-list of clauses, and the default last", () => {
    render(<BranchCases step={step} />);
    const rows = screen.getAllByTestId("branch-case");
    expect(rows.map((r) => r.getAttribute("data-case"))).toEqual(["1", "2", "3", "default"]);
    expect(rows[0]).toHaveTextContent("1steps.feed.output.ok eq false→ r_feed_failed");
    expect(within(rows[1]).getAllByTestId("branch-clause").map((c) => c.textContent)).toEqual(["input.dry eq true", "input.ignore_posted eq true"]);
    expect(rows[1]).toHaveTextContent("all(");
    expect(rows[1]).toHaveTextContent("→ ltn");
    expect(rows[2]).toHaveTextContent("any(");
    expect(rows[2]).toHaveTextContent("all(");
    expect(rows[3]).toHaveTextContent("default→ ltn");
    expect(rows.some((r) => r.hasAttribute("data-taken"))).toBe(false);
  });

  it("highlights the taken case, or the default when case is null", () => {
    const { rerender } = render(<BranchCases step={step} taken={branchTaken({ case: 1, next: "ltn" })} />);
    expect(screen.getAllByTestId("branch-case").map((r) => r.hasAttribute("data-taken"))).toEqual([false, true, false, false]);
    rerender(<BranchCases step={step} taken={branchTaken({ case: null, next: "ltn" })} />);
    expect(screen.getAllByTestId("branch-case").map((r) => r.hasAttribute("data-taken"))).toEqual([false, false, false, true]);
    expect(branchTaken("nope")).toBeNull();
  });

  it("shows a condition_unknown warning as an amber note on that case only", () => {
    render(<BranchCases step={step} taken={{ case: null, next: "ltn" }} warnings={[{ code: "condition_unknown", condition: "cases.2" }]} />);
    const rows = screen.getAllByTestId("branch-case");
    expect(within(rows[2]).getByTestId("branch-case-unknown")).toHaveTextContent("An input was not given");
    expect(screen.getAllByTestId("branch-case-unknown")).toHaveLength(1);
  });
});

const spec: WorkflowSpec = {
  name: "Gate",
  start: "gate",
  steps: [
    { id: "gate", type: "branch", cases: [{ when: { op: "eq", left: ref("input.a"), right: 1 }, next: "a" }], default: "b" },
    { id: "a", type: "tool", tool_id: "t", next: "end" },
    { id: "b", type: "tool", tool_id: "t", next: "end" },
    { id: "end", type: "tool", tool_id: "t" },
  ],
} as unknown as WorkflowSpec;
const states: Record<string, NodeState> = { gate: "completed", a: "skipped", b: "completed", end: "skipped" };
const details = {
  gate: { stepId: "gate", output: { case: null, next: "b" }, hasOutput: true, warnings: [], skipReason: null },
  a: { stepId: "a", skipReason: "branch_not_taken" },
  end: { stepId: "end", skipReason: "run_ended" },
} as unknown as Record<string, StepDetail>;

describe("not reached steps", () => {
  it("labels them Not reached, with a tooltip saying why", () => {
    expect(NODE_STATE_LABELS.skipped).toBe("Not reached");
    expect(notReachedHint("run_ended")).toBe("The run ended before this step.");
    expect(notReachedHint("branch_not_taken")).toBe("This branch arm was not taken.");
    render(<WorkflowStepList spec={spec} states={states} details={details} legend />);
    expect(within(screen.getByRole("list", { name: "Node state legend" })).getByText("Not reached")).toBeInTheDocument();
    expect(screen.queryByText("Skipped")).toBeNull();
  });

  it("folds into one row, closed by default, that opens on click and is remembered", () => {
    render(<WorkflowStepList spec={spec} states={states} details={details} onSelectStep={() => undefined} foldNotReached />);
    const toggle = screen.getByTestId("workflow-step-not-reached-toggle");
    expect(toggle).toHaveTextContent("2 steps not reached");
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByRole("button", { name: /Step a,/ })).toBeNull();
    expect(screen.getByRole("button", { name: "Step gate, Branch, Completed" })).toBeInTheDocument();
    fireEvent.click(toggle);
    expect(toggle).toHaveAttribute("aria-expanded", "true");
    expect(window.localStorage.getItem(NOT_REACHED_OPEN_KEY)).toBe("true");
    expect(screen.getByRole("button", { name: "Step a, Tool, Not reached" })).toBeInTheDocument();
    const label = screen.getByTestId("workflow-step-a").querySelector("[title]");
    expect(label).toHaveAttribute("title", "This branch arm was not taken.");
    expect(screen.getByTestId("workflow-step-end").querySelector("[title]")).toHaveAttribute("title", "The run ended before this step.");
  });

  it("starts open when the browser remembers it, and shows the taken case in the list", () => {
    window.localStorage.setItem(NOT_REACHED_OPEN_KEY, "true");
    render(<WorkflowStepList spec={spec} states={states} details={details} onSelectStep={() => undefined} foldNotReached />);
    expect(screen.getByTestId("workflow-step-not-reached-toggle")).toHaveAttribute("aria-expanded", "true");
    const rows = within(screen.getByTestId("workflow-step-gate")).getAllByTestId("branch-case");
    expect(rows.map((r) => r.hasAttribute("data-taken"))).toEqual([false, true]);
  });

  it("opens the group for a selected not-reached step, and shows no group without folding", () => {
    const { rerender } = render(<WorkflowStepList spec={spec} states={states} details={details} selectedStepId="a" onSelectStep={() => undefined} foldNotReached />);
    expect(screen.getByTestId("workflow-step-not-reached-toggle")).toHaveAttribute("aria-expanded", "true");
    rerender(<WorkflowStepList spec={spec} states={states} details={details} />);
    expect(screen.queryByTestId("workflow-step-not-reached-toggle")).toBeNull();
  });
});
