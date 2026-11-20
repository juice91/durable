import React from "react";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import type { RunEvent, SubscribeHandlers } from "@/lib/runs";
import { ApiError } from "@/lib/api";
import { demoSpec, parallelApprovalSpec } from "@/test/workflowFixtures";
import { routes } from "@/lib/routes";

const runsMock = vi.hoisted(() => ({
  getRun: vi.fn(),
  listEvents: vi.fn(),
  subscribeRun: vi.fn(),
}));
const wfMock = vi.hoisted(() => ({ getWorkflowVersion: vi.fn(), decideApproval: vi.fn() }));

vi.mock("next/link", () => ({ default: ({ href, children, ...rest }: { href: string; children: React.ReactNode }) => <a href={href} {...rest}>{children}</a> }));
vi.mock("@/lib/runs", async (importOriginal) => ({ ...(await importOriginal<object>()), ...runsMock }));
vi.mock("@/lib/workflows", async (importOriginal) => ({ ...(await importOriginal<object>()), ...wfMock }));

import { WorkflowRunView } from "./WorkflowRunView";

beforeAll(() => {
  // React Flow measures its container.
  class RO { observe() {} unobserve() {} disconnect() {} }
  vi.stubGlobal("ResizeObserver", RO);
  class DM { m22 = 1; constructor() {} }
  vi.stubGlobal("DOMMatrixReadOnly", DM);
});

let handlers: SubscribeHandlers;
let unsubscribe: ReturnType<typeof vi.fn>;
let seq = 0;
const ev = (type: RunEvent["type"], data: Record<string, unknown> = {}, at?: number): RunEvent => ({ seq: at ?? seq++, type, data, ts: "2026-11-13T10:00:00Z" });

const RUN = {
  run_id: "run-1", kind: "workflow", workflow_id: "wf-1", workflow_version: 1, agent_id: null, version: null,
  status: "running", display_status: "running", input: "{}", output: null, error: null, heartbeat_at: null,
  created_at: "2026-11-13T10:00:00Z", updated_at: "2026-11-13T10:00:05Z", trace_id: null, conversation_id: null, turn: null,
};

beforeEach(() => {
  seq = 0;
  unsubscribe = vi.fn();
  runsMock.getRun.mockResolvedValue(RUN);
  runsMock.listEvents.mockResolvedValue([]);
  runsMock.subscribeRun.mockImplementation((_id: string, h: SubscribeHandlers) => {
    handlers = h;
    return unsubscribe;
  });
  wfMock.getWorkflowVersion.mockResolvedValue({ workflow_id: "wf-1", version: 1, spec: demoSpec, spec_hash: "h", created_at: "", created_by: "u" });
  wfMock.decideApproval.mockReset();
});

afterEach(() => cleanup());

async function renderView() {
  render(<WorkflowRunView runId="run-1" workflowId="wf-1" />);
  await screen.findByTestId("workflow-step-classify");
}

const stepState = (id: string) => screen.getByTestId(`workflow-step-${id}`).getAttribute("data-state");

describe("WorkflowRunView", () => {
  it("colours nodes from the stream, ignoring duplicates and out-of-order events", async () => {
    await renderView();
    expect(stepState("classify")).toBe("pending");
    const started = ev("step_started", { step_id: "classify", step_type: "agent", input: { text: "hi" }, child_run_id: "child-9" });
    const done = ev("step_completed", { step_id: "classify", output: "billing" });
    act(() => {
      handlers.onEvent?.(done);
      handlers.onEvent?.(started);
      handlers.onEvent?.(done);
    });
    expect(stepState("classify")).toBe("completed");
    expect(screen.getByTestId("workflow-node-classify")).toHaveAttribute("data-state", "completed");
    act(() => handlers.onEvent?.(ev("step_started", { step_id: "lookup", step_type: "tool" })));
    expect(stepState("lookup")).toBe("running");
    expect(screen.getByTestId("workflow-run-stream")).toHaveAttribute("data-state", "live");

    fireEvent.click(within(screen.getByTestId("workflow-step-classify")).getByRole("button", { name: /^Step / }));
    const panel = screen.getByTestId("step-io-panel");
    expect(within(panel).getByTestId("step-io-input")).toHaveTextContent('"text": "hi"');
    expect(within(panel).getByTestId("step-io-output")).toHaveTextContent("billing");
    expect(within(panel).getByTestId("step-child-run-link")).toHaveAttribute("href", routes.run("child-9"));
  });

  it("approves a waiting step with a comment, then refreshes", async () => {
    await renderView();
    act(() => handlers.onEvent?.(ev("step_waiting", { step_id: "approve", prompt: "Escalate?", deadline: "2026-11-13T14:00:00Z", timeout_hours: 4 })));
    expect(stepState("approve")).toBe("waiting");
    const card = screen.getByTestId("approval-approve");
    expect(within(card).getByTestId("approval-prompt-approve")).toHaveTextContent("Escalate?");
    wfMock.decideApproval.mockResolvedValue({ run_id: "run-1", step_id: "approve", decision: "approved", decided_by: "dev", comment: "ok", notified: true });
    runsMock.listEvents.mockResolvedValue([
      ev("step_waiting", { step_id: "approve", prompt: "Escalate?" }, 0),
      ev("step_approved", { step_id: "approve", decided_by: "dev", comment: "ok" }, 1),
    ]);
    fireEvent.change(within(card).getByLabelText("Comment (optional)"), { target: { value: "ok" } });
    fireEvent.click(within(card).getByRole("button", { name: "Approve" }));
    await waitFor(() => expect(wfMock.decideApproval).toHaveBeenCalledWith("run-1", "approve", "approve", "ok"));
    await waitFor(() => expect(screen.queryByTestId("approval-approve")).toBeNull());
    fireEvent.click(within(screen.getByTestId("workflow-step-approve")).getByRole("button", { name: /^Step / }));
    expect(screen.getByTestId("step-decided-by")).toHaveTextContent("dev");
    expect(screen.getByTestId("step-decision-comment")).toHaveTextContent("ok");
  });

  it("rejects through a confirmation dialog", async () => {
    await renderView();
    act(() => handlers.onEvent?.(ev("step_waiting", { step_id: "approve", prompt: "?" })));
    wfMock.decideApproval.mockResolvedValue({ decision: "rejected" });
    fireEvent.click(within(screen.getByTestId("approval-approve")).getByRole("button", { name: "Reject" }));
    const dialog = await screen.findByRole("dialog");
    expect(wfMock.decideApproval).not.toHaveBeenCalled();
    fireEvent.click(within(dialog).getByRole("button", { name: "Reject" }));
    await waitFor(() => expect(wfMock.decideApproval).toHaveBeenCalledWith("run-1", "approve", "reject", ""));
  });

  it("explains a 409 and refreshes the run state", async () => {
    await renderView();
    act(() => handlers.onEvent?.(ev("step_waiting", { step_id: "approve", prompt: "?" })));
    const before = runsMock.listEvents.mock.calls.length;
    wfMock.decideApproval.mockRejectedValue(new ApiError(409, "step is not waiting for approval"));
    fireEvent.click(within(screen.getByTestId("approval-approve")).getByRole("button", { name: "Approve" }));
    expect(await screen.findByTestId("approval-error-approve")).toHaveTextContent(/no longer waiting/);
    await waitFor(() => expect(runsMock.listEvents.mock.calls.length).toBeGreaterThan(before));
  });

  it("limits the comment to 2000 characters", async () => {
    await renderView();
    act(() => handlers.onEvent?.(ev("step_waiting", { step_id: "approve", prompt: "?" })));
    const card = screen.getByTestId("approval-approve");
    fireEvent.change(within(card).getByLabelText("Comment (optional)"), { target: { value: "x".repeat(2001) } });
    expect(within(card).getByRole("button", { name: "Approve" })).toBeDisabled();
  });

  it("shows the terminal status of a run that ended with no step events", async () => {
    await renderView();
    act(() => {
      handlers.onEvent?.(ev("run_failed", { status: "failed", error: "bad spec", error_code: "workflow_spec_invalid" }));
      handlers.onEnd?.("run_failed");
    });
    expect(screen.getByTestId("workflow-run-no-steps")).toHaveTextContent("failed");
    expect(screen.getByTestId("workflow-run-error-code")).toHaveTextContent("workflow_spec_invalid");
    expect(screen.getByTestId("workflow-run-view")).toHaveAttribute("data-status", "failed");
  });

  it("shows needs_attention clearly", async () => {
    await renderView();
    act(() => handlers.onEvent?.(ev("run_failed", { status: "needs_attention", error: "unknown", error_code: "side_effect_outcome_unknown", step_id: "lookup" })));
    expect(screen.getByTestId("workflow-run-needs-attention")).toBeInTheDocument();
    expect(stepState("lookup")).toBe("needs_attention");
  });

  it("shows an approval inside a parallel branch: gate waiting, sibling running, join pending", async () => {
    wfMock.getWorkflowVersion.mockResolvedValue({ workflow_id: "wf-1", version: 1, spec: parallelApprovalSpec, spec_hash: "pa", created_at: "", created_by: "u" });
    render(<WorkflowRunView runId="run-1" workflowId="wf-1" />);
    await screen.findByTestId("workflow-step-gate");
    act(() => {
      handlers.onEvent?.(ev("step_started", { step_id: "fork", step_type: "parallel" }));
      handlers.onEvent?.(ev("step_started", { step_id: "gate", step_type: "approval" }));
      handlers.onEvent?.(ev("step_waiting", { step_id: "gate", prompt: "Ship branch A?", deadline: "2026-11-13T12:00:00Z", timeout_hours: 2 }));
      handlers.onEvent?.(ev("step_started", { step_id: "b", step_type: "tool", tool_id: "x" }));
    });
    expect(stepState("fork")).toBe("running");
    expect(stepState("gate")).toBe("waiting");
    expect(stepState("b")).toBe("running");
    expect(stepState("merge")).toBe("pending");
    const card = screen.getByTestId("approval-gate");
    expect(within(card).getByTestId("approval-prompt-gate")).toHaveTextContent("Ship branch A?");
    expect(within(card).getByRole("button", { name: "Approve" })).toBeEnabled();

    // The sibling finishing does not release the join while the gate still waits.
    act(() => handlers.onEvent?.(ev("step_completed", { step_id: "b", output: { ok: true } })));
    expect(stepState("b")).toBe("completed");
    expect(stepState("gate")).toBe("waiting");
    expect(stepState("merge")).toBe("pending");
    expect(screen.getByTestId("approval-gate")).toBeInTheDocument();
  });

  it("labels a parallel step's null output instead of printing null", async () => {
    wfMock.getWorkflowVersion.mockResolvedValue({ workflow_id: "wf-1", version: 1, spec: parallelApprovalSpec, spec_hash: "pa", created_at: "", created_by: "u" });
    render(<WorkflowRunView runId="run-1" workflowId="wf-1" />);
    await screen.findByTestId("workflow-step-fork");
    act(() => {
      handlers.onEvent?.(ev("step_started", { step_id: "fork", step_type: "parallel" }));
      handlers.onEvent?.(ev("step_completed", { step_id: "fork", output: null }));
    });
    fireEvent.click(within(screen.getByTestId("workflow-step-fork")).getByRole("button", { name: /^Step / }));
    const output = within(screen.getByTestId("step-io-panel")).getByTestId("step-io-output");
    expect(output).toHaveTextContent("No output (parallel step).");
    expect(output).not.toHaveTextContent(/^null$/);
  });

  it("reports a missing run and stops following it on unmount", async () => {
    const { unmount } = render(<WorkflowRunView runId="run-1" />);
    await screen.findByTestId("workflow-step-classify");
    unmount();
    expect(unsubscribe).toHaveBeenCalled();
    expect(() => handlers.onEvent?.(ev("step_started", { step_id: "classify" }))).not.toThrow();

    runsMock.getRun.mockRejectedValue(new ApiError(404, "Run not found"));
    render(<WorkflowRunView runId="run-2" />);
    expect(await screen.findByTestId("workflow-run-not-found")).toBeInTheDocument();
  });
});
