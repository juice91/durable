import React from "react";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { demoSpec } from "@/test/workflowFixtures";

const wfMock = vi.hoisted(() => ({ startWorkflowRun: vi.fn() }));
vi.mock("next/link", () => ({ default: ({ href, children, ...rest }: { href: string; children: React.ReactNode }) => <a href={href} {...rest}>{children}</a> }));
vi.mock("@/lib/workflows", async (importOriginal) => ({ ...(await importOriginal<object>()), ...wfMock }));

import { WorkflowRunPanel } from "./WorkflowRunPanel";

beforeEach(() => {
  wfMock.startWorkflowRun.mockReset();
  wfMock.startWorkflowRun.mockResolvedValue({ run_id: "run-9" });
});
afterEach(() => cleanup());

const panel = (onStarted = vi.fn()) => render(<WorkflowRunPanel workflowId="wf-1" workflowName="Demo" version={1} spec={demoSpec} onStarted={onStarted} />);

describe("WorkflowRunPanel step by step", () => {
  it("offers a Step by step switch with its help text, off by default", () => {
    panel();
    const toggle = screen.getByRole("switch", { name: "Step by step" });
    expect(toggle).toHaveAttribute("aria-checked", "false");
    expect(screen.getByText("Pause to inspect outputs")).toBeInTheDocument();
  });

  it("sends pause_after_each_step when the switch is on", async () => {
    const onStarted = vi.fn();
    panel(onStarted);
    fireEvent.click(screen.getByRole("switch", { name: "Step by step" }));
    fireEvent.click(screen.getByRole("button", { name: "Run workflow" }));
    await waitFor(() => expect(onStarted).toHaveBeenCalledWith("run-9"));
    expect(wfMock.startWorkflowRun).toHaveBeenCalledWith("wf-1", expect.anything(), expect.objectContaining({ version: 1, pauseAfterEachStep: true }));
  });

  it("does not ask for pauses when the switch is off", async () => {
    const onStarted = vi.fn();
    panel(onStarted);
    fireEvent.click(screen.getByRole("button", { name: "Run workflow" }));
    await waitFor(() => expect(onStarted).toHaveBeenCalled());
    expect(wfMock.startWorkflowRun.mock.calls[0][2].pauseAfterEachStep).toBeFalsy();
  });
});
