import React from "react";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "@/lib/api";
import type { Run, RunEvent, SubscribeHandlers } from "@/lib/runs";
import { demoSpec, parallelSpec } from "@/test/workflowFixtures";

const runsMock = vi.hoisted(() => ({
  getRun: vi.fn(),
  listEvents: vi.fn(),
  subscribeRun: vi.fn(),
}));
const wfMock = vi.hoisted(() => ({ getWorkflowVersion: vi.fn(), decideApproval: vi.fn() }));

vi.mock("next/link", () => ({
  default: ({ href, children, ...rest }: { href: string; children: React.ReactNode }) => <a href={href} {...rest}>{children}</a>,
}));
vi.mock("@/lib/runs", async (importOriginal) => ({ ...(await importOriginal<object>()), ...runsMock }));
vi.mock("@/lib/workflows", async (importOriginal) => ({ ...(await importOriginal<object>()), ...wfMock }));

import { WorkflowRunView } from "./WorkflowRunView";

beforeAll(() => {
  class RO { observe() {} unobserve() {} disconnect() {} }
  vi.stubGlobal("ResizeObserver", RO);
  vi.stubGlobal("DOMMatrixReadOnly", class { m22 = 1; });
});

let handlers: SubscribeHandlers;
let unsubscribe: ReturnType<typeof vi.fn>;
let seq = 0;

const ev = (type: RunEvent["type"], data: Record<string, unknown> = {}, at?: number): RunEvent => ({
  seq: at ?? seq++,
  type,
  data,
  ts: "2026-11-13T10:00:00Z",
});

const RUN: Run = {
  run_id: "run-1",
  kind: "workflow",
  workflow_id: "wf-1",
  workflow_version: 1,
  agent_id: null,
  version: null,
  status: "running",
  display_status: "running",
  input: "{}",
  output: null,
  error: null,
  heartbeat_at: null,
  created_at: "2026-11-13T10:00:00Z",
  updated_at: "2026-11-13T10:00:05Z",
  trace_id: null,
  conversation_id: null,
  turn: null,
};

function emit(...events: RunEvent[]) {
  act(() => {
    for (const event of events) handlers.onEvent?.(event);
  });
}

function expectState(id: string, state: string) {
  expect(screen.getByTestId(`workflow-step-${id}`), id).toHaveAttribute("data-state", state);
  expect(screen.getByTestId(`workflow-node-${id}`), id).toHaveAttribute("data-state", state);
}

beforeEach(() => {
  seq = 0;
  handlers = {};
  unsubscribe = vi.fn();
  runsMock.getRun.mockReset();
  runsMock.listEvents.mockReset();
  runsMock.subscribeRun.mockReset();
  runsMock.getRun.mockResolvedValue(RUN);
  runsMock.listEvents.mockResolvedValue([]);
  runsMock.subscribeRun.mockImplementation((_id: string, next: SubscribeHandlers) => {
    handlers = next;
    return unsubscribe;
  });
  wfMock.getWorkflowVersion.mockReset();
  wfMock.getWorkflowVersion.mockResolvedValue({
    workflow_id: "wf-1", version: 1, spec: demoSpec, spec_hash: "h", created_at: "", created_by: "u",
  });
  wfMock.decideApproval.mockReset();
});

afterEach(() => cleanup());

async function renderView(waitForStep = "classify") {
  render(<WorkflowRunView runId="run-1" workflowId="wf-1" />);
  await screen.findByTestId(`workflow-step-${waitForStep}`);
}

describe("WorkflowRunView acceptance", () => {
  it("walks node states through agent, tool, branch, approval and the final agent", async () => {
    await renderView();
    for (const id of ["classify", "lookup", "route", "approve", "summarize"]) expectState(id, "pending");
    expect(screen.getByTestId("workflow-run-stream")).toHaveAttribute("data-state", "connecting");
    expect(screen.getByRole("button", { name: "Step classify, Agent, Pending" })).toBeInTheDocument();

    emit(ev("step_started", { step_id: "classify", step_type: "agent", input: { text: "hi" }, child_run_id: "child-9-abcdefgh" }));
    expectState("classify", "running");
    expect(screen.getByTestId("workflow-run-stream")).toHaveAttribute("data-state", "live");
    expect(screen.getByRole("button", { name: "Step classify, Agent, Running" })).toBeInTheDocument();

    emit(ev("step_completed", { step_id: "classify", output: "billing" }));
    expectState("classify", "completed");
    fireEvent.click(screen.getByRole("button", { name: "Step classify, Agent, Completed" }));
    const panel = screen.getByTestId("step-io-panel");
    expect(panel).toHaveAttribute("data-step-id", "classify");
    expect(within(panel).getByTestId("step-io-input")).toHaveTextContent('"text": "hi"');
    expect(within(panel).getByTestId("step-io-output")).toHaveTextContent("billing");
    expect(within(panel).getByTestId("step-child-run-link")).toHaveAttribute("href", "/runs/child-9-abcdefgh");
    expect(within(panel).getByTestId("step-io-state")).toHaveTextContent("Completed");

    emit(ev("step_started", { step_id: "lookup", step_type: "tool", tool_id: "http.get", input: { url: "https://example.test" } }));
    expectState("lookup", "running");
    emit(ev("step_completed", { step_id: "lookup", output: { ok: true } }));
    expectState("lookup", "completed");

    emit(ev("step_started", { step_id: "route", step_type: "branch" }));
    expectState("route", "running");
    emit(ev("step_completed", { step_id: "route", output: { case: 1, next: "approve" } }));
    expectState("route", "completed");
    expectState("approve", "pending");
    expectState("summarize", "pending");

    emit(ev("step_started", { step_id: "approve", step_type: "approval" }));
    emit(ev("step_waiting", { step_id: "approve", prompt: "Escalate?", timeout_hours: 4, deadline: "2026-11-13T14:00:00Z" }));
    expectState("approve", "waiting");
    expect(screen.getByTestId("approval-prompt-approve")).toHaveTextContent("Escalate?");
    expect(screen.getByTestId("approval-deadline-approve")).toHaveAttribute("datetime", "2026-11-13T14:00:00Z");

    emit(ev("step_approved", { step_id: "approve", decided_by: "ada", comment: "ship it" }));
    expectState("approve", "running");
    emit(ev("step_completed", { step_id: "approve", output: { approved: true, decided_by: "ada", comment: "ship it" } }));
    expectState("approve", "completed");
    expect(screen.queryByTestId("approval-approve")).not.toBeInTheDocument();

    emit(ev("step_started", { step_id: "summarize", step_type: "agent", input: { text: "billing" }, child_run_id: "child-sum" }));
    expectState("summarize", "running");
    emit(ev("step_completed", { step_id: "summarize", output: "done" }));
    expectState("summarize", "completed");
    act(() => {
      handlers.onEvent?.(ev("run_completed", { output: "done", result: "done" }));
      handlers.onEnd?.("run_completed");
    });
    expect(screen.getByTestId("workflow-run-view")).toHaveAttribute("data-status", "completed");
    expect(screen.getByTestId("workflow-run-output")).toHaveTextContent("done");
    expect(screen.getByTestId("workflow-run-stream")).toHaveAttribute("data-state", "ended");
    expectState("classify", "completed");
    expectState("approve", "completed");
    expectState("summarize", "completed");
  });

  it("applies duplicate and out-of-order events by seq and still shows step I/O", async () => {
    await renderView();
    const started = ev("step_started", {
      step_id: "classify", step_type: "agent", input: { text: "hi" }, child_run_id: "child-9",
    }, 1);
    const done = ev("step_completed", { step_id: "classify", output: "billing" }, 2);

    emit(done, started, done);
    expectState("classify", "completed");
    expectState("lookup", "pending");
    fireEvent.click(screen.getByRole("button", { name: "Step classify, Agent, Completed" }));
    expect(screen.getByTestId("step-io-input")).toHaveTextContent('"text": "hi"');
    expect(screen.getByTestId("step-io-output")).toHaveTextContent("billing");
    expect(screen.getByTestId("step-child-run-link")).toHaveAttribute("href", "/runs/child-9");

    cleanup();
    await renderView();
    emit(started, done, started);
    expectState("classify", "completed");
    fireEvent.click(screen.getByRole("button", { name: "Step classify, Agent, Completed" }));
    expect(screen.getByTestId("step-io-input")).toHaveTextContent('"text": "hi"');
    expect(screen.getByTestId("step-io-output")).toHaveTextContent("billing");
    expect(screen.getByTestId("step-child-run-link")).toHaveAttribute("href", "/runs/child-9");
  });

  it("keeps the graph across a reconnect and does not open a second subscription", async () => {
    await renderView();
    const started = ev("step_started", { step_id: "lookup", step_type: "tool", tool_id: "http.get" }, 4);
    const done = ev("step_completed", { step_id: "lookup", output: { ok: true } }, 5);
    emit(started);
    expectState("lookup", "running");
    expect(screen.getByTestId("workflow-run-stream")).toHaveAttribute("data-state", "live");

    act(() => handlers.onError?.(new Error("socket closed")));
    expect(screen.getByTestId("workflow-run-stream")).toHaveAttribute("data-state", "reconnecting");
    expectState("lookup", "running");
    expect(runsMock.subscribeRun).toHaveBeenCalledTimes(1);

    emit(started);
    expectState("lookup", "running");
    emit(done);
    expectState("lookup", "completed");
    expect(screen.getByTestId("workflow-run-stream")).toHaveAttribute("data-state", "live");
    expect(runsMock.subscribeRun).toHaveBeenCalledTimes(1);
  });

  it("shows a run that ends before any step ran", async () => {
    await renderView();
    act(() => {
      handlers.onEvent?.(ev("run_failed", { status: "failed", error: "bad spec", error_code: "workflow_spec_invalid" }));
      handlers.onEnd?.("run_failed");
    });
    expect(screen.getByTestId("workflow-run-view")).toHaveAttribute("data-status", "failed");
    expect(screen.getByTestId("workflow-run-no-steps")).toHaveTextContent("failed");
    expect(screen.getByTestId("workflow-run-no-steps")).toHaveTextContent("before any step ran");
    expect(screen.getByTestId("workflow-run-failed")).toHaveTextContent("bad spec");
    expect(screen.getByTestId("workflow-run-error-code")).toHaveTextContent("workflow_spec_invalid");
    expect(screen.getByTestId("workflow-run-stream")).toHaveAttribute("data-state", "ended");
    for (const id of ["classify", "lookup", "route", "approve", "summarize"]) expectState(id, "pending");
  });

  it("skips the branch arm that was not taken and leaves the taken arm runnable", async () => {
    await renderView();
    emit(
      ev("step_started", { step_id: "route", step_type: "branch" }),
      ev("step_completed", { step_id: "route", output: { case: null, next: "summarize" } }),
    );
    expectState("route", "completed");
    expectState("approve", "skipped");
    expectState("summarize", "pending");
    expect(screen.getByRole("button", { name: "Step approve, Approval, Skipped" })).toBeInTheDocument();
    expect(screen.getByTestId("workflow-run-view")).toHaveAttribute("data-status", "running");

    emit(
      ev("step_started", { step_id: "summarize", step_type: "agent" }),
      ev("step_completed", { step_id: "summarize", output: "short" }),
    );
    act(() => handlers.onEvent?.(ev("run_completed", { result: "short" })));
    expectState("summarize", "completed");
    expectState("approve", "skipped");
    expectState("classify", "skipped");
    expectState("lookup", "skipped");
    expect(screen.getByTestId("workflow-run-view")).toHaveAttribute("data-status", "completed");
  });

  it("runs parallel branches and does not skip the join while a branch is open", async () => {
    wfMock.getWorkflowVersion.mockResolvedValue({
      workflow_id: "wf-1", version: 1, spec: parallelSpec, spec_hash: "p", created_at: "", created_by: "u",
    });
    await renderView("fork");
    expect(screen.getByTestId("workflow-node-fork")).toHaveAttribute("data-step-type", "parallel");

    emit(
      ev("step_started", { step_id: "fork", step_type: "parallel" }),
      ev("step_completed", { step_id: "fork" }),
      ev("step_started", { step_id: "a", step_type: "tool", tool_id: "x" }),
      ev("step_started", { step_id: "b", step_type: "tool", tool_id: "x" }),
    );
    expectState("fork", "completed");
    expectState("a", "running");
    expectState("b", "running");
    expectState("merge", "pending");

    emit(ev("step_completed", { step_id: "b", output: 1 }));
    expectState("b", "completed");
    expectState("a", "running");
    expectState("merge", "pending");
    expectState("a2", "pending");

    emit(
      ev("step_completed", { step_id: "a", output: 2 }),
      ev("step_started", { step_id: "a2", step_type: "tool", tool_id: "x" }),
      ev("step_completed", { step_id: "a2", output: 3 }),
    );
    expectState("a2", "completed");
    expectState("merge", "pending");

    emit(ev("step_started", { step_id: "merge", step_type: "agent", child_run_id: "child-merge" }));
    expectState("merge", "running");
    emit(ev("step_completed", { step_id: "merge", output: "joined" }));
    act(() => handlers.onEvent?.(ev("run_completed", { result: "joined" })));
    expectState("merge", "completed");
    expectState("fork", "completed");
    expectState("a", "completed");
    expectState("b", "completed");
    expect(screen.getByTestId("workflow-run-view")).toHaveAttribute("data-status", "completed");
    expect(screen.getByTestId("workflow-run-output")).toHaveTextContent("joined");

    fireEvent.click(screen.getByRole("button", { name: "Step merge, Agent, Completed" }));
    expect(screen.getByTestId("step-child-run-link")).toHaveAttribute("href", "/runs/child-merge");
    expect(screen.getByTestId("step-io-output")).toHaveTextContent("joined");
  });

  it("shows a failed step and its error code", async () => {
    await renderView();
    emit(ev("step_started", { step_id: "lookup", step_type: "tool", tool_id: "http.get", input: {} }));
    expectState("lookup", "running");
    emit(ev("step_failed", {
      step_id: "lookup", status: "failed", error: "upstream timed out", error_code: "tool_timeout",
    }));
    act(() => handlers.onEvent?.(ev("run_failed", {
      status: "failed", error: "upstream timed out", error_code: "tool_timeout", step_id: "lookup",
    })));
    expectState("lookup", "failed");
    expect(screen.getByTestId("workflow-run-view")).toHaveAttribute("data-status", "failed");
    expect(screen.getByTestId("workflow-run-failed")).toHaveTextContent("Failed at step lookup");
    expect(screen.getByTestId("workflow-run-failed")).toHaveTextContent("upstream timed out");
    expect(screen.getByTestId("workflow-run-error-code")).toHaveTextContent("tool_timeout");
    expect(screen.queryByTestId("workflow-run-needs-attention")).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Step lookup, Tool, Failed" }));
    const error = screen.getByTestId("step-io-error");
    expect(error).toHaveTextContent("upstream timed out");
    expect(error).toHaveTextContent("tool_timeout");
    expect(screen.getByTestId("step-io-panel")).toHaveAttribute("data-step-id", "lookup");
  });

  it("shows needs_attention and does not start another run", async () => {
    await renderView();
    emit(ev("step_started", { step_id: "lookup", step_type: "tool", tool_id: "http.get" }));
    emit(ev("run_failed", {
      status: "needs_attention",
      error: "unknown outcome",
      error_code: "side_effect_outcome_unknown",
      step_id: "lookup",
    }));
    expect(screen.getByTestId("workflow-run-view")).toHaveAttribute("data-status", "needs_attention");
    expect(screen.getByTestId("workflow-run-needs-attention")).toHaveTextContent("not retried automatically");
    expect(screen.getByTestId("workflow-run-needs-attention")).toHaveTextContent("unknown outcome");
    expect(screen.queryByTestId("workflow-run-failed")).not.toBeInTheDocument();
    expectState("lookup", "needs_attention");
    expect(runsMock.subscribeRun).toHaveBeenCalledTimes(1);

    fireEvent.click(screen.getByRole("button", { name: "Step lookup, Tool, Needs attention" }));
    expect(screen.getByTestId("step-io-error")).toHaveTextContent("side_effect_outcome_unknown");
    expect(screen.getByTestId("step-io-state")).toHaveTextContent("Needs attention");
  });

  it("approves with a comment and shows who decided", async () => {
    await renderView();
    emit(ev("step_waiting", { step_id: "approve", prompt: "Escalate?", deadline: "2026-11-13T14:00:00Z" }, 0));
    const card = screen.getByRole("region", { name: "Approval for step approve" });
    expect(within(card).getByTestId("approval-prompt-approve")).toHaveTextContent("Escalate?");
    wfMock.decideApproval.mockImplementation(async () => {
      runsMock.listEvents.mockResolvedValue([
        ev("step_waiting", { step_id: "approve", prompt: "Escalate?" }, 0),
        ev("step_approved", { step_id: "approve", decided_by: "ada", comment: "ship it" }, 1),
        ev("step_completed", { step_id: "approve", output: { approved: true, decided_by: "ada", comment: "ship it" } }, 2),
      ]);
      return { run_id: "run-1", step_id: "approve", decision: "approved", decided_by: "ada", comment: "ship it", notified: true };
    });
    fireEvent.change(within(card).getByLabelText("Comment (optional)"), { target: { value: "ship it" } });
    fireEvent.click(within(card).getByRole("button", { name: "Approve" }));
    await waitFor(() => expect(wfMock.decideApproval).toHaveBeenCalledWith("run-1", "approve", "approve", "ship it"));
    await waitFor(() => expect(screen.queryByTestId("approval-approve")).not.toBeInTheDocument());
    expectState("approve", "completed");
    fireEvent.click(screen.getByRole("button", { name: "Step approve, Approval, Completed" }));
    expect(screen.getByTestId("step-decision")).toHaveAttribute("data-decision", "approved");
    expect(screen.getByTestId("step-decided-by")).toHaveTextContent("ada");
    expect(screen.getByTestId("step-decision-comment")).toHaveTextContent("ship it");
  });

  it("rejects through the confirmation dialog and shows the decision", async () => {
    await renderView();
    emit(ev("step_waiting", { step_id: "approve", prompt: "Escalate?" }, 0));
    const card = screen.getByTestId("approval-approve");
    fireEvent.change(within(card).getByLabelText("Comment (optional)"), { target: { value: "nope" } });
    fireEvent.click(within(card).getByRole("button", { name: "Reject" }));
    const dialog = await screen.findByRole("dialog");
    expect(dialog).toHaveTextContent("Reject step approve?");
    expect(wfMock.decideApproval).not.toHaveBeenCalled();
    wfMock.decideApproval.mockImplementation(async () => {
      runsMock.listEvents.mockResolvedValue([
        ev("step_waiting", { step_id: "approve", prompt: "Escalate?" }, 0),
        ev("step_rejected", { step_id: "approve", decided_by: "ada", comment: "nope" }, 1),
        ev("step_failed", { step_id: "approve", status: "failed", error: "rejected", error_code: "approval_rejected" }, 2),
        ev("run_failed", { status: "failed", error: "rejected", error_code: "approval_rejected", step_id: "approve" }, 3),
      ]);
      return { run_id: "run-1", step_id: "approve", decision: "rejected", decided_by: "ada", comment: "nope", notified: true };
    });
    fireEvent.click(within(dialog).getByRole("button", { name: "Reject" }));
    await waitFor(() => expect(wfMock.decideApproval).toHaveBeenCalledWith("run-1", "approve", "reject", "nope"));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    await waitFor(() => expect(screen.queryByTestId("approval-approve")).not.toBeInTheDocument());
    expectState("approve", "failed");
    fireEvent.click(screen.getByRole("button", { name: "Step approve, Approval, Failed" }));
    expect(screen.getByTestId("step-decision")).toHaveAttribute("data-decision", "rejected");
    expect(screen.getByTestId("step-decided-by")).toHaveTextContent("ada");
    expect(screen.getByTestId("step-decision-comment")).toHaveTextContent("nope");
    expect(screen.getByTestId("step-io-error")).toHaveTextContent("approval_rejected");
  });

  it("refreshes on a 409 and shows a not-found message on a 404", async () => {
    await renderView();
    emit(ev("step_waiting", { step_id: "approve", prompt: "?" }, 0));
    const readsBefore = runsMock.listEvents.mock.calls.length;
    const runsBefore = runsMock.getRun.mock.calls.length;
    wfMock.decideApproval.mockRejectedValueOnce(new ApiError(409, "step is not waiting for approval"));
    fireEvent.click(within(screen.getByTestId("approval-approve")).getByRole("button", { name: "Approve" }));
    expect(await screen.findByTestId("approval-error-approve")).toHaveTextContent(/no longer waiting/);
    await waitFor(() => expect(runsMock.listEvents.mock.calls.length).toBeGreaterThan(readsBefore));
    await waitFor(() => expect(runsMock.getRun.mock.calls.length).toBeGreaterThan(runsBefore));
    expect(screen.getByTestId("approval-approve")).toBeInTheDocument();

    const readsAfterConflict = runsMock.listEvents.mock.calls.length;
    wfMock.decideApproval.mockRejectedValueOnce(new ApiError(404, "Run not found"));
    fireEvent.click(within(screen.getByTestId("approval-approve")).getByRole("button", { name: "Approve" }));
    expect(await screen.findByTestId("approval-error-approve")).toHaveTextContent(
      "Run not found: it may have been removed, or it belongs to another tenant.",
    );
    await act(async () => { await new Promise((resolve) => setTimeout(resolve, 20)); });
    expect(runsMock.listEvents.mock.calls.length).toBe(readsAfterConflict);
    expect(screen.getByTestId("approval-approve")).toBeInTheDocument();
  });

  it("disables Approve and Reject when the comment is 2001 characters", async () => {
    await renderView();
    emit(ev("step_waiting", { step_id: "approve", prompt: "?" }));
    const card = screen.getByTestId("approval-approve");
    const comment = within(card).getByLabelText("Comment (optional)");
    fireEvent.change(comment, { target: { value: "x".repeat(2000) } });
    expect(within(card).getByRole("button", { name: "Approve" })).toBeEnabled();
    expect(within(card).getByRole("button", { name: "Reject" })).toBeEnabled();

    fireEvent.change(comment, { target: { value: "x".repeat(2001) } });
    expect(card).toHaveTextContent("2001/2000");
    expect(comment).toHaveAttribute("aria-invalid", "true");
    expect(within(card).getByRole("button", { name: "Approve" })).toBeDisabled();
    expect(within(card).getByRole("button", { name: "Reject" })).toBeDisabled();
    fireEvent.click(within(card).getByRole("button", { name: "Approve" }));
    fireEvent.click(within(card).getByRole("button", { name: "Reject" }));
    expect(wfMock.decideApproval).not.toHaveBeenCalled();
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("unsubscribes on unmount and applies no stream updates after that", async () => {
    let resolveRun: (run: Run) => void = () => undefined;
    let resolveEvents: (events: RunEvent[]) => void = () => undefined;
    runsMock.getRun.mockImplementation(() => new Promise((resolve) => { resolveRun = resolve; }));
    runsMock.listEvents.mockImplementation(() => new Promise((resolve) => { resolveEvents = resolve; }));
    unsubscribe.mockImplementation(() => {
      handlers.onEvent?.(ev("step_started", { step_id: "classify", step_type: "agent" }, 1));
      handlers.onStatus?.({ display_status: "completed" });
      handlers.onError?.(new Error("closed"));
      handlers.onEnd?.("run_completed");
    });

    const { unmount } = render(<WorkflowRunView runId="run-1" workflowId="wf-1" />);
    await waitFor(() => expect(runsMock.subscribeRun).toHaveBeenCalledWith("run-1", expect.any(Object)));
    expect(screen.getByTestId("workflow-run-view")).toBeInTheDocument();
    unmount();
    expect(unsubscribe).toHaveBeenCalledTimes(1);
    expect(screen.queryByTestId("workflow-run-view")).not.toBeInTheDocument();

    const spy = vi.spyOn(console, "error").mockImplementation(() => undefined);
    try {
      await act(async () => {
        resolveRun(RUN);
        resolveEvents([ev("step_started", { step_id: "classify", step_type: "agent", input: { text: "late" } }, 2)]);
        handlers.onEvent?.(ev("step_completed", { step_id: "classify", output: "late" }, 3));
        handlers.onStatus?.({ display_status: "failed" });
        handlers.onError?.(new Error("late"));
        handlers.onEnd?.("run_failed");
        await Promise.resolve();
      });
      expect(screen.queryByTestId("workflow-run-view")).not.toBeInTheDocument();
      expect(screen.queryByTestId("workflow-node-classify")).not.toBeInTheDocument();
      const leaked = spy.mock.calls
        .map((args) => args.map(String).join(" "))
        .filter((line) => /unmounted|not wrapped in act/i.test(line));
      expect(leaked).toEqual([]);
    } finally {
      spy.mockRestore();
    }
  });
});
