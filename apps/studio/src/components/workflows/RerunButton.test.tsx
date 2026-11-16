import React from "react";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ToolRunError } from "@/lib/toolRuns";

const wfMock = vi.hoisted(() => ({ startWorkflowRun: vi.fn() }));
vi.mock("@/lib/workflows", async (importOriginal) => ({ ...(await importOriginal<object>()), ...wfMock }));

import { RerunButton } from "./RerunButton";

const RUN = { kind: "workflow" as const, workflow_id: "wf-1", workflow_version: 3, input: '{"text":"hi","n":[1,2]}' };

beforeEach(() => wfMock.startWorkflowRun.mockReset());
afterEach(() => cleanup());

describe("RerunButton", () => {
  it("posts the same input on the same version and reports the new run id", async () => {
    wfMock.startWorkflowRun.mockResolvedValue({ run_id: "new-run" });
    const onStarted = vi.fn();
    render(<RerunButton run={RUN} onStarted={onStarted} />);
    fireEvent.click(screen.getByRole("button", { name: "Run again" }));
    await waitFor(() => expect(onStarted).toHaveBeenCalledWith("new-run"));
    expect(wfMock.startWorkflowRun).toHaveBeenCalledTimes(1);
    expect(wfMock.startWorkflowRun).toHaveBeenCalledWith("wf-1", { text: "hi", n: [1, 2] }, { version: 3 });
  });

  it("is disabled while starting", async () => {
    let resolve!: (v: { run_id: string }) => void;
    wfMock.startWorkflowRun.mockReturnValue(new Promise((r) => (resolve = r)));
    render(<RerunButton run={RUN} onStarted={vi.fn()} />);
    fireEvent.click(screen.getByTestId("workflow-run-again"));
    const button = await screen.findByRole("button", { name: "Starting…" });
    expect(button).toBeDisabled();
    fireEvent.click(button);
    expect(wfMock.startWorkflowRun).toHaveBeenCalledTimes(1);
    resolve({ run_id: "r2" });
    await waitFor(() => expect(screen.getByRole("button", { name: "Run again" })).toBeEnabled());
  });

  it("shows a start error and does not navigate", async () => {
    wfMock.startWorkflowRun.mockRejectedValueOnce(new ToolRunError(404, "Workflow or version not found", {}));
    const onStarted = vi.fn();
    render(<RerunButton run={RUN} onStarted={onStarted} />);
    fireEvent.click(screen.getByRole("button", { name: "Run again" }));
    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toBe("Workflow or version not found");
    expect(onStarted).not.toHaveBeenCalled();
    await waitFor(() => expect(screen.getByRole("button", { name: "Run again" })).toBeEnabled());
  });

  it("renders nothing for a run without a readable input or version", () => {
    const { container } = render(<RerunButton run={{ ...RUN, input: "oops" }} onStarted={vi.fn()} />);
    expect(container.textContent).toBe("");
    cleanup();
    expect(render(<RerunButton run={{ ...RUN, workflow_version: null }} onStarted={vi.fn()} />).container.textContent).toBe("");
  });
});
