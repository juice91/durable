// LP.6 review: the view re-fits after a collapse or expand (not only on the first render).
import React from "react";
import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { foreachSpec } from "@/test/workflowLoopFixtures";

const fitView = vi.fn();
vi.mock("@xyflow/react", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@xyflow/react")>();
  return {
    ...actual,
    useReactFlow: () => ({ ...actual.useReactFlow(), fitView }),
  };
});

import { WorkflowCanvas } from "./WorkflowGraph";

beforeEach(() => {
  vi.stubGlobal("DOMMatrixReadOnly", class { m22 = 1; });
  fitView.mockClear();
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

const frame = () => act(() => new Promise<void>((resolve) => requestAnimationFrame(() => resolve())));

describe("WorkflowCanvas re-fit", () => {
  it("fits the view after each collapse and expand, never on mount", async () => {
    render(<div style={{ width: 800, height: 600 }}><WorkflowCanvas spec={foreachSpec} /></div>);
    await frame();
    expect(fitView).not.toHaveBeenCalled();
    fireEvent.click(screen.getByTestId("workflow-loops-toggle"));
    await frame();
    expect(fitView).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByTestId("workflow-loops-toggle"));
    await frame();
    expect(fitView).toHaveBeenCalledTimes(2);
  });

  it("fits when a controlled set changes from outside (Back, a link)", async () => {
    const { rerender } = render(<div style={{ width: 800, height: 600 }}><WorkflowCanvas spec={foreachSpec} collapsedLoops={new Set()} /></div>);
    await frame();
    rerender(<div style={{ width: 800, height: 600 }}><WorkflowCanvas spec={foreachSpec} collapsedLoops={new Set()} /></div>);
    await frame();
    expect(fitView).not.toHaveBeenCalled(); // a new but equal set is not a change
    rerender(<div style={{ width: 800, height: 600 }}><WorkflowCanvas spec={foreachSpec} collapsedLoops={new Set(["each"])} /></div>);
    await frame();
    expect(fitView).toHaveBeenCalledTimes(1);
  });
});
