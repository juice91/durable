import React from "react";
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { WorkflowCanvas, WorkflowStepList } from "./WorkflowGraph";
import { AXE_TIMEOUT_MS, runAxe } from "@/test/a11y";
import { demoSpec } from "@/test/workflowFixtures";
import { foreachSpec, nestedSpec, refineSpec, untilSpec } from "@/test/workflowLoopFixtures";

beforeEach(() => {
  // React Flow reads the viewport transform through DOMMatrixReadOnly, which jsdom lacks.
  vi.stubGlobal("DOMMatrixReadOnly", class { m22 = 1; });
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("WorkflowCanvas loop groups (LP.6)", () => {
  it("draws each loop as a group with its badge and summary, body nodes inside it", () => {
    render(<div style={{ width: 800, height: 600 }}><WorkflowCanvas spec={nestedSpec} /></div>);
    const outer = screen.getByTestId("workflow-node-outer");
    expect(outer).toHaveAttribute("data-group", "true");
    expect(outer).toHaveAttribute("data-collapsed", "false");
    expect(screen.getByTestId("workflow-loop-badge-outer")).toHaveTextContent("×20");
    expect(screen.getByTestId("workflow-loop-summary-inner")).toHaveTextContent("while all(loop.inner.index lt 3, not(loop.inner.state.stop exists))");
    for (const id of ["inner", "tally", "leaf"]) expect(screen.getByTestId(`workflow-node-${id}`)).toBeInTheDocument();
    const graph = screen.getByTestId("workflow-graph");
    expect(graph.querySelector(".react-flow__renderer")).toHaveAttribute("aria-label", "Execution graph of Nested with 7 steps, 2 loops");
  });

  it("collapses and expands every loop from the zoom bar, and one loop from its chevron", () => {
    render(<div style={{ width: 800, height: 600 }}><WorkflowCanvas spec={foreachSpec} /></div>);
    const toggle = screen.getByTestId("workflow-loops-toggle");
    expect(toggle).toHaveAccessibleName("Collapse all loops");
    expect(toggle).toHaveAttribute("aria-pressed", "false");
    fireEvent.click(toggle);
    expect(screen.queryByTestId("workflow-node-triage")).toBeNull();
    expect(screen.getByTestId("workflow-node-each")).toHaveAttribute("data-collapsed", "true");
    expect(screen.getByTestId("workflow-loop-badge-each")).toHaveTextContent("×50");
    // The name stays; only aria-pressed reports the state.
    expect(screen.getByTestId("workflow-loops-toggle")).toHaveAccessibleName("Collapse all loops");
    expect(screen.getByTestId("workflow-loops-toggle")).toHaveAttribute("aria-pressed", "true");
    fireEvent.click(screen.getByTestId("workflow-loops-toggle"));
    expect(screen.getByTestId("workflow-node-triage")).toBeInTheDocument();

    const onSelect = vi.fn();
    cleanup();
    render(<div style={{ width: 800, height: 600 }}><WorkflowCanvas spec={foreachSpec} onSelectStep={onSelect} /></div>);
    const chevron = screen.getByTestId("workflow-node-each").querySelector("[data-loop-toggle]")!;
    fireEvent.click(chevron);
    expect(screen.queryByTestId("workflow-node-triage")).toBeNull();
    expect(onSelect).not.toHaveBeenCalled();
    fireEvent.click(screen.getByTestId("workflow-node-each"));
    expect(onSelect).toHaveBeenCalledWith("each");
  });

  it("takes a controlled collapsed set and run-view badges", () => {
    const onChange = vi.fn();
    render(
      <div style={{ width: 800, height: 600 }}>
        <WorkflowCanvas spec={untilSpec} collapsedLoops={new Set(["poll"])} onCollapsedLoopsChange={onChange} loopBadges={{ poll: "×7" }} />
      </div>,
    );
    expect(screen.getByTestId("workflow-node-poll")).toHaveAttribute("data-collapsed", "true");
    expect(screen.getByTestId("workflow-loop-badge-poll")).toHaveTextContent("×7");
    fireEvent.click(screen.getByTestId("workflow-loops-toggle"));
    expect(onChange).toHaveBeenCalledWith(new Set());
    // Controlled: nothing changes until the parent passes a new set.
    expect(screen.getByTestId("workflow-node-poll")).toHaveAttribute("data-collapsed", "true");
  });

  it("shows no loops toggle for a loop-free spec", () => {
    render(<div style={{ width: 800, height: 600 }}><WorkflowCanvas spec={demoSpec} /></div>);
    expect(screen.queryByTestId("workflow-loops-toggle")).toBeNull();
  });

  it("has no axe violations with groups", async () => {
    const { container } = render(<div style={{ width: 375, height: 600 }}><WorkflowCanvas spec={refineSpec} onSelectStep={() => {}} /></div>);
    const results = await runAxe(container);
    expect(results.violations).toEqual([]);
  }, AXE_TIMEOUT_MS);
});

describe("WorkflowStepList loops (LP.6)", () => {
  it("indents body steps in a nested list under their loop, at every depth", () => {
    render(<WorkflowStepList spec={nestedSpec} />);
    const top = screen.getByTestId("workflow-step-list");
    expect([...top.children].map((li) => li.getAttribute("data-testid"))).toEqual([
      "workflow-step-fetch",
      "workflow-step-outer",
      "workflow-step-route",
      "workflow-step-done",
    ]);
    const outerBody = screen.getByTestId("workflow-step-body-outer");
    expect(outerBody).toHaveAccessibleName("Body of outer");
    expect(within(outerBody).getByTestId("workflow-step-inner")).toHaveAttribute("data-depth", "1");
    expect(within(screen.getByTestId("workflow-step-body-inner")).getByTestId("workflow-step-leaf")).toHaveAttribute("data-depth", "2");
    expect(screen.getByTestId("workflow-step-outer")).toHaveTextContent("Foreach ×20");
    expect(screen.getByTestId("workflow-step-outer")).toHaveTextContent("each of steps.fetch.output.tickets · max 20 · one at a time · collect errors");
    expect(screen.getByTestId("workflow-step-leaf")).toHaveTextContent("ends the iteration");
    expect(screen.getByTestId("workflow-step-route")).toHaveTextContent("→ done (1: any(input.a eq 1, input.b exists))");
  });

  it("labels evaluate and unknown kinds, and names loop buttons with their bound", async () => {
    const odd = { ...refineSpec, steps: [...refineSpec.steps.map((s) => ({ ...s, next: "later" })), { id: "later", type: "someday", next: null }] } as unknown as typeof refineSpec;
    const { container } = render(<WorkflowStepList spec={odd} onSelectStep={() => {}} />);
    expect(screen.getByTestId("workflow-step-judge")).toHaveTextContent("Evaluate");
    expect(screen.getByTestId("workflow-step-later")).toHaveTextContent("someday");
    expect(screen.getByRole("button", { name: "Step refine, Loop, up to 3 times" })).toBeInTheDocument();
    const results = await runAxe(container);
    expect(results.violations).toEqual([]);
  }, AXE_TIMEOUT_MS);
});

describe("LP.6 review fixes", () => {
  it("a collapsed loop's subtitle says what it repeats, never the bound (the badge carries it)", () => {
    render(<div style={{ width: 800, height: 600 }}><WorkflowCanvas spec={foreachSpec} collapsedLoops={new Set(["each"])} loopBadges={{ each: "×7" }} /></div>);
    const node = screen.getByTestId("workflow-node-each");
    expect(node).toHaveTextContent("each of steps.fetch.output.tickets");
    expect(node.textContent).not.toContain("×50");
    expect(screen.getByTestId("workflow-loop-badge-each")).toHaveTextContent("×7");
  });

  it("collapses one loop from the keyboard in the step list, shared with the canvas", () => {
    function Harness() {
      const [collapsed, setCollapsed] = React.useState<ReadonlySet<string>>(new Set());
      return (
        <>
          <WorkflowStepList spec={nestedSpec} collapsedLoops={collapsed} onCollapsedLoopsChange={setCollapsed} />
          <div style={{ width: 800, height: 600 }}><WorkflowCanvas spec={nestedSpec} collapsedLoops={collapsed} onCollapsedLoopsChange={setCollapsed} /></div>
        </>
      );
    }
    render(<Harness />);
    const toggle = screen.getByTestId("workflow-loop-toggle-inner");
    expect(toggle.tagName).toBe("BUTTON");
    expect(toggle).toHaveAccessibleName("Steps of inner");
    expect(toggle).toHaveAttribute("aria-expanded", "true");
    expect(toggle.getAttribute("aria-controls")).toBe(screen.getByTestId("workflow-step-body-inner").id);
    toggle.focus();
    fireEvent.click(toggle);
    expect(screen.getByTestId("workflow-loop-toggle-inner")).toHaveAttribute("aria-expanded", "false");
    expect(screen.getByTestId("workflow-step-body-inner")).not.toBeVisible();
    expect(screen.getByTestId("workflow-step-inner")).toHaveAttribute("data-collapsed", "true");
    // The canvas follows: the inner loop is one node, its body gone; the outer stays open.
    expect(screen.getByTestId("workflow-node-inner")).toHaveAttribute("data-collapsed", "true");
    expect(screen.queryByTestId("workflow-node-leaf")).toBeNull();
    expect(screen.getByTestId("workflow-node-outer")).toHaveAttribute("data-collapsed", "false");
    // And back from the canvas's collapse-all: every list toggle reports collapsed.
    fireEvent.click(screen.getByTestId("workflow-loops-toggle"));
    expect(screen.getByTestId("workflow-loop-toggle-outer")).toHaveAttribute("aria-expanded", "false");
  });

  it("the step list's toggles pass axe", async () => {
    const { container } = render(<WorkflowStepList spec={nestedSpec} collapsedLoops={new Set(["inner"])} onCollapsedLoopsChange={() => {}} onSelectStep={() => {}} />);
    const results = await runAxe(container);
    expect(results.violations).toEqual([]);
  }, AXE_TIMEOUT_MS);
});
