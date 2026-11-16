import React from "react";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import type { RunEvent, SubscribeHandlers } from "@/lib/runs";
import { ApiError } from "@/lib/api";
import { demoSpec } from "@/test/workflowFixtures";

const runsMock = vi.hoisted(() => ({ getRun: vi.fn(), listEvents: vi.fn(), subscribeRun: vi.fn() }));
const wfMock = vi.hoisted(() => ({ getWorkflowVersion: vi.fn(), decideApproval: vi.fn() }));
vi.mock("next/link", () => ({ default: ({ href, children, ...rest }: { href: string; children: React.ReactNode }) => <a href={href} {...rest}>{children}</a> }));
vi.mock("@/lib/runs", async (importOriginal) => ({ ...(await importOriginal<object>()), ...runsMock }));
vi.mock("@/lib/workflows", async (importOriginal) => ({ ...(await importOriginal<object>()), ...wfMock }));

import { WorkflowRunView } from "./WorkflowRunView";

beforeAll(() => {
  class RO { observe() {} unobserve() {} disconnect() {} }
  vi.stubGlobal("ResizeObserver", RO);
  class DM { m22 = 1; constructor() {} }
  vi.stubGlobal("DOMMatrixReadOnly", DM);
});

let handlers: SubscribeHandlers;
let seq = 0;
const ev = (type: RunEvent["type"], data: Record<string, unknown> = {}): RunEvent => ({ seq: seq++, type, data, ts: "2026-11-13T10:00:00Z" });
const RUN = {
  run_id: "run-1", kind: "workflow", workflow_id: "wf-1", workflow_version: 1, agent_id: null, version: null,
  status: "waiting_approval", display_status: "waiting_approval", input: "{}", output: null, error: null, heartbeat_at: null,
  created_at: "2026-11-13T10:00:00Z", updated_at: "2026-11-13T10:00:05Z", trace_id: null, conversation_id: null, turn: null,
  pause_after_each_step: true,
};

beforeEach(() => {
  seq = 0;
  runsMock.getRun.mockResolvedValue(RUN);
  runsMock.listEvents.mockResolvedValue([]);
  runsMock.subscribeRun.mockImplementation((_id: string, h: SubscribeHandlers) => {
    handlers = h;
    return vi.fn();
  });
  wfMock.getWorkflowVersion.mockResolvedValue({ workflow_id: "wf-1", version: 1, spec: demoSpec, spec_hash: "h", created_at: "", created_by: "u" });
  wfMock.decideApproval.mockReset();
  wfMock.decideApproval.mockResolvedValue({ run_id: "run-1", step_id: "classify_pause", decision: "approved", decided_by: "dev", comment: null, notified: true });
});
afterEach(() => cleanup());

async function renderPaused() {
  render(<WorkflowRunView runId="run-1" workflowId="wf-1" />);
  await screen.findByTestId("workflow-step-classify");
  act(() => {
    handlers.onEvent?.(ev("step_started", { step_id: "classify", step_type: "agent", input: { text: "hi" } }));
    handlers.onEvent?.(ev("step_completed", { step_id: "classify", output: "billing" }));
    handlers.onEvent?.(ev("step_waiting", { step_id: "classify_pause", prompt: "Paused after classify", pause: true, timeout_hours: 24, workflow_id: "wf-1", deadline: "2026-11-14T10:00:00Z" }));
  });
}

describe("WorkflowRunView paused after a step", () => {
  it("shows a Paused after banner and opens the step's output in the IO panel", async () => {
    await renderPaused();
    const banner = await screen.findByTestId("workflow-paused-banner");
    expect(banner).toHaveTextContent("Paused after classify");
    expect(screen.queryByTestId("approval-classify_pause")).toBeNull();
    const io = screen.getByTestId("step-io-panel");
    expect(within(io).getByTestId("step-io-output")).toHaveTextContent("billing");
  });

  it("Continue approves the synthetic pause step", async () => {
    await renderPaused();
    fireEvent.click(within(await screen.findByTestId("workflow-paused-banner")).getByRole("button", { name: "Continue" }));
    await waitFor(() => expect(wfMock.decideApproval).toHaveBeenCalledWith("run-1", "classify_pause", "approve"));
  });

  it("Stop rejects after a confirmation", async () => {
    await renderPaused();
    fireEvent.click(within(await screen.findByTestId("workflow-paused-banner")).getByRole("button", { name: "Stop" }));
    const dialog = await screen.findByRole("dialog");
    expect(wfMock.decideApproval).not.toHaveBeenCalled();
    fireEvent.click(within(dialog).getByRole("button", { name: "Stop run" }));
    await waitFor(() => expect(wfMock.decideApproval).toHaveBeenCalledWith("run-1", "classify_pause", "reject"));
  });

  it("says the run is paused in the overlay header", async () => {
    await renderPaused();
    expect(await screen.findByText("Paused. Continue or stop the run.")).toBeInTheDocument();
    expect(screen.getByTestId("workflow-paused-banner")).toHaveTextContent("open in the step panel");
  });

  it("removes the banner once the pause step is approved", async () => {
    await renderPaused();
    await screen.findByTestId("workflow-paused-banner");
    act(() => {
      handlers.onEvent?.(ev("step_approved", { step_id: "classify_pause", decided_by: "dev" }));
      handlers.onEvent?.(ev("step_completed", { step_id: "classify_pause", output: null }));
    });
    await waitFor(() => expect(screen.queryByTestId("workflow-paused-banner")).toBeNull());
  });

  it("removes the banner when the run is terminal", async () => {
    await renderPaused();
    await screen.findByTestId("workflow-paused-banner");
    act(() => handlers.onEvent?.(ev("run_failed", { error_code: "pause_timed_out" })));
    await waitFor(() => expect(screen.queryByTestId("workflow-paused-banner")).toBeNull());
  });

  it("disables Continue and Stop while the decision is pending", async () => {
    let finish!: () => void;
    wfMock.decideApproval.mockReturnValue(new Promise<void>((resolve) => { finish = resolve; }));
    await renderPaused();
    const banner = await screen.findByTestId("workflow-paused-banner");
    fireEvent.click(within(banner).getByRole("button", { name: "Continue" }));
    await waitFor(() => expect(within(banner).getByRole("button", { name: "Continue" })).toBeDisabled());
    expect(within(banner).getByRole("button", { name: "Stop" })).toBeDisabled();
    expect(wfMock.decideApproval).toHaveBeenCalledTimes(1);
    await act(async () => { finish(); });
  });

  it("explains a 409 on Continue", async () => {
    wfMock.decideApproval.mockRejectedValue(new ApiError(409, "not waiting"));
    await renderPaused();
    fireEvent.click(within(await screen.findByTestId("workflow-paused-banner")).getByRole("button", { name: "Continue" }));
    expect(await screen.findByTestId("workflow-paused-error")).toHaveTextContent(/no longer paused/);
  });

  it("shows a generic failure on Continue", async () => {
    wfMock.decideApproval.mockRejectedValue(new Error("boom"));
    await renderPaused();
    fireEvent.click(within(await screen.findByTestId("workflow-paused-banner")).getByRole("button", { name: "Continue" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("boom");
  });
});
