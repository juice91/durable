// LP.6 tester acceptance: step list order, badges, keyboard, axe, phone-width classes.
import React from "react";
import fs from "node:fs";
import path from "node:path";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { WorkflowCanvas, WorkflowStepList } from "./WorkflowGraph";
import { buildWorkflowGraph } from "@/lib/workflowGraph";
import { AXE_TIMEOUT_MS, runAxe } from "@/test/a11y";
import { checkLayout } from "@/test/layoutGuard";
import { demoSpec } from "@/test/workflowFixtures";
import { foreachSpec, nestedSpec, refineSpec, untilSpec } from "@/test/workflowLoopFixtures";

beforeEach(() => {
  vi.stubGlobal("DOMMatrixReadOnly", class { m22 = 1; });
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

const box = (el: React.ReactElement, w = 375) => <div style={{ width: w, height: 600 }}>{el}</div>;

describe("step list matches graph order", () => {
  it.each([["foreach", foreachSpec], ["nested", nestedSpec], ["refine", refineSpec], ["demo", demoSpec]])("%s", (_n, spec) => {
    render(<WorkflowStepList spec={spec} />);
    const listIds = [...screen.getByTestId("workflow-step-list").querySelectorAll("[data-testid^='workflow-step-']")]
      .map((el) => el.getAttribute("data-testid")!)
      .filter((t) => !t.startsWith("workflow-step-body-") && t !== "workflow-step-list")
      .map((t) => t.replace("workflow-step-", ""));
    const graphIds = buildWorkflowGraph(spec).nodes.map((n) => n.id);
    // The same steps in exactly the same order (a pre-order walk: each loop, then its body).
    expect(listIds).toEqual(graphIds);
    // A loop's body steps are one contiguous block right after the loop, in the list's nesting too.
    const nodes = buildWorkflowGraph(spec).nodes;
    for (const loop of nodes.filter((n) => n.kind === "group")) {
      const at = listIds.indexOf(loop.id);
      const descendants = new Set<string>();
      for (const n of nodes) if (n.parentId === loop.id || (n.parentId !== null && descendants.has(n.parentId))) descendants.add(n.id);
      expect(descendants.size).toBeGreaterThan(0);
      expect(new Set(listIds.slice(at + 1, at + 1 + descendants.size))).toEqual(descendants);
      const body = screen.getByTestId(`workflow-step-body-${loop.id}`);
      for (const id of descendants) expect(body).toContainElement(screen.getByTestId(`workflow-step-${id}`));
    }
  });

  it("depth attributes grow with nesting and loop-free lists have only depth 0", () => {
    const { unmount } = render(<WorkflowStepList spec={nestedSpec} />);
    const depths = Object.fromEntries([...document.querySelectorAll("[data-depth]")].map((e) => [e.getAttribute("data-testid"), e.getAttribute("data-depth")]));
    expect(depths).toMatchObject({ "workflow-step-outer": "0", "workflow-step-inner": "1", "workflow-step-leaf": "2", "workflow-step-tally": "1" });
    unmount();
    render(<WorkflowStepList spec={demoSpec} />);
    expect(new Set([...document.querySelectorAll("[data-depth]")].map((e) => e.getAttribute("data-depth")))).toEqual(new Set(["0"]));
  });
});

describe("badges", () => {
  it("each loop group shows ×bound, foreach by max_items and loop by max_iterations", () => {
    render(box(<WorkflowCanvas spec={nestedSpec} />, 800));
    expect(screen.getByTestId("workflow-loop-badge-outer")).toHaveTextContent("×20");
    expect(screen.getByTestId("workflow-loop-badge-inner")).toHaveTextContent("×5");
  });

  it("collapsed loops keep their badge; loop-free specs have none", () => {
    render(box(<WorkflowCanvas spec={untilSpec} collapsedLoops={new Set(["poll"])} />, 800));
    expect(screen.getByTestId("workflow-loop-badge-poll")).toHaveTextContent("×10");
    cleanup();
    render(box(<WorkflowCanvas spec={demoSpec} />, 800));
    expect(document.querySelector("[data-testid^='workflow-loop-badge-']")).toBeNull();
  });
});

describe("keyboard and accessibility", () => {
  it("the collapse-all control is a real focusable button that toggles aria-pressed on Enter/click", () => {
    render(box(<WorkflowCanvas spec={nestedSpec} />, 800));
    const btn = screen.getByTestId("workflow-loops-toggle");
    expect(btn.tagName).toBe("BUTTON");
    expect(btn).toHaveAttribute("aria-pressed", "false");
    btn.focus();
    expect(document.activeElement).toBe(btn);
    fireEvent.click(btn);
    expect(screen.getByTestId("workflow-loops-toggle")).toHaveAttribute("aria-pressed", "true");
    expect(screen.queryByTestId("workflow-node-leaf")).toBeNull();
    expect(screen.getByTestId("workflow-node-outer")).toHaveAttribute("data-collapsed", "true");
  });

  it("tab order reaches the collapse control among the zoom buttons", () => {
    render(box(<WorkflowCanvas spec={foreachSpec} />, 800));
    const tabbable = [...document.querySelectorAll<HTMLElement>("button, [tabindex='0']")].filter((e) => e.tabIndex >= 0);
    expect(tabbable).toContain(screen.getByTestId("workflow-loops-toggle"));
  });

  it.each([["expanded", new Set<string>()], ["collapsed", new Set(["outer"])]])("axe passes at 375 px for nested loops, %s", async (_n, collapsed) => {
    const { container } = render(box(<WorkflowCanvas spec={nestedSpec} collapsedLoops={collapsed} onSelectStep={() => {}} />));
    expect((await runAxe(container)).violations).toEqual([]);
  }, AXE_TIMEOUT_MS);

  it("axe passes on the nested step list", async () => {
    const { container } = render(<WorkflowStepList spec={nestedSpec} onSelectStep={() => {}} />);
    expect((await runAxe(container)).violations).toEqual([]);
  }, AXE_TIMEOUT_MS);
});

describe("phone width (375 px), checked by class", () => {
  const src = fs.readFileSync(path.resolve(__dirname, "WorkflowGraph.tsx"), "utf8");

  it("the layout guard finds no violations in WorkflowGraph.tsx", () => {
    expect(checkLayout("components/workflows/WorkflowGraph.tsx", src)).toEqual([]);
  });

  it("canvas and step list are min-w-0 / overflow-contained; no fixed pixel width or vw units", () => {
    render(<WorkflowStepList spec={nestedSpec} />);
    expect(screen.getByTestId("workflow-step-list").parentElement).toHaveClass("min-w-0");
    expect(screen.getByTestId("workflow-step-body-outer").className).not.toMatch(/\bw-\[?\d/);
    expect(src).not.toMatch(/100[dsl]?vw|w-screen/);
    cleanup();
    render(box(<WorkflowCanvas spec={nestedSpec} />));
    expect(screen.getByTestId("workflow-canvas")).toHaveClass("min-w-0", "w-full", "overflow-hidden");
  });

  it("long loop summary text truncates or wraps rather than widening", () => {
    render(<WorkflowStepList spec={nestedSpec} />);
    expect(screen.getByTestId("workflow-step-inner").innerHTML).toMatch(/break-words|truncate/);
    cleanup();
    render(box(<WorkflowCanvas spec={nestedSpec} />, 800));
    expect(screen.getByTestId("workflow-loop-summary-inner")).toHaveClass("truncate");
  });
});
