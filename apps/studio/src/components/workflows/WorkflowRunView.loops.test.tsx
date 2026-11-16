// LP.12: the run view's iteration pager, loop iteration table, Timeline iteration groups and Cancel.
import React from "react";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import type { RunEvent, SubscribeHandlers } from "@/lib/runs";
import { ApiError } from "@/lib/api";
import { foreachSpec, nestedSpec } from "@/test/workflowLoopFixtures";
import { expectNoAxeViolations, AXE_TIMEOUT_MS } from "@/test/a11y";
import { setWindowWidth } from "@/components/frame/frameTesting";

const runsMock = vi.hoisted(() => ({ getRun: vi.fn(), listEvents: vi.fn(), subscribeRun: vi.fn() }));
const wfMock = vi.hoisted(() => ({ getWorkflowVersion: vi.fn(), decideApproval: vi.fn(), cancelRun: vi.fn() }));

vi.mock("next/link", () => ({ default: ({ href, children, ...rest }: { href: string; children: React.ReactNode }) => <a href={href} {...rest}>{children}</a> }));
vi.mock("@/lib/runs", async (importOriginal) => ({ ...(await importOriginal<object>()), ...runsMock }));
vi.mock("@/lib/workflows", async (importOriginal) => ({ ...(await importOriginal<object>()), ...wfMock }));

import { WorkflowRunView } from "./WorkflowRunView";

let handlers: SubscribeHandlers;
let seq = 0;
const ev = (type: string, data: Record<string, unknown> = {}, ts = "2026-11-15T10:00:00Z"): RunEvent => ({ seq: seq++, type: type as RunEvent["type"], data, ts });
const emit = (...events: RunEvent[]) => act(() => events.forEach((event) => handlers.onEvent?.(event)));

const RUN = {
  run_id: "run-1", kind: "workflow", workflow_id: "wf-1", workflow_version: 1, agent_id: null, version: null,
  status: "running", display_status: "running", input: "{}", output: null, error: null, heartbeat_at: null,
  created_at: "2026-11-15T10:00:00Z", updated_at: "2026-11-15T10:00:05Z", trace_id: null, conversation_id: null, turn: null,
};

function iteration(index: number, outcome: "completed" | "failed" | "running"): RunEvent[] {
  const scope = [{ loop: "each", index }];
  const out = [
    ev("loop_iteration_started", { step_id: "each", scope: [], index }),
    ev("step_started", { step_id: "triage", step_type: "agent", scope }),
  ];
  if (outcome === "failed") {
    out.push(ev("step_failed", { step_id: "triage", scope, status: "failed", error: "boom", error_code: "model_error" }));
    out.push(ev("loop_iteration_failed", { step_id: "each", scope: [], index, status: "failed", error: "boom", error_code: "model_error", failed_step_id: "triage" }));
  } else if (outcome === "completed") {
    out.push(ev("step_completed", { step_id: "triage", scope, output: `t${index}` }));
    out.push(ev("step_started", { step_id: "update", step_type: "tool", scope }));
    out.push(ev("step_completed", { step_id: "update", scope, output: {} }));
    out.push(ev("loop_iteration_completed", { step_id: "each", scope: [], index, state: null }));
  }
  return out;
}

const threeIterations = () => [
  ev("step_started", { step_id: "fetch", step_type: "tool" }),
  ev("step_completed", { step_id: "fetch", output: { tickets: [1, 2, 3] } }),
  ev("step_started", { step_id: "each", step_type: "foreach" }),
  ...iteration(0, "completed"),
  ...iteration(1, "failed"),
  ...iteration(2, "running"),
];

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
  wfMock.getWorkflowVersion.mockResolvedValue({ workflow_id: "wf-1", version: 1, spec: foreachSpec, spec_hash: "h", created_at: "", created_by: "u" });
  wfMock.cancelRun.mockReset();
});

afterEach(() => {
  cleanup();
  restoreWidth();
  restoreWidth = () => undefined;
});

async function renderView() {
  render(<WorkflowRunView runId="run-1" workflowId="wf-1" />);
  await screen.findByTestId("workflow-step-triage");
}

const stepState = (id: string) => screen.getByTestId(`workflow-step-${id}`).getAttribute("data-state");
const pager = () => screen.getByTestId("workflow-iteration-pager-each");
const pagerLabel = () => within(pager()).getByTestId("workflow-iteration-pager-label").textContent;

describe("iteration pager", () => {
  it("shows the latest iteration, pages with the buttons and the arrow keys, and returns to the latest", async () => {
    await renderView();
    emit(...threeIterations());
    expect(pagerLabel()).toBe("Iteration 3 of 3");
    expect(stepState("triage")).toBe("running");
    expect(screen.getByTestId("workflow-node-triage")).toHaveAttribute("data-state", "running");
    expect(screen.getByTestId("workflow-loop-badge-each")).toHaveTextContent("×3");

    fireEvent.click(within(pager()).getByRole("button", { name: "Previous iteration" }));
    expect(pagerLabel()).toBe("Iteration 2 of 3");
    expect(stepState("triage")).toBe("failed");
    expect(stepState("update")).toBe("pending");

    fireEvent.keyDown(within(pager()).getByRole("button", { name: "Next iteration" }), { key: "ArrowLeft" });
    expect(pagerLabel()).toBe("Iteration 1 of 3");
    expect(stepState("triage")).toBe("completed");
    expect(within(pager()).getByRole("button", { name: "Previous iteration" })).toHaveAttribute("aria-disabled", "true");

    // A new iteration does not move a pinned pager; Latest follows again.
    emit(...iteration(3, "running"));
    expect(pagerLabel()).toBe("Iteration 1 of 4");
    fireEvent.click(within(pager()).getByTestId("workflow-iteration-pager-latest"));
    expect(pagerLabel()).toBe("Iteration 4 of 4");
    expect(within(pager()).getByTestId("workflow-iteration-pager-latest")).toHaveAttribute("aria-disabled", "true");
  });

  it("'Failed only' jumps to a failed iteration and pages through failed ones", async () => {
    await renderView();
    emit(...threeIterations());
    const toggle = within(pager()).getByRole("switch", { name: "Failed only (1)" });
    fireEvent.click(toggle);
    expect(pagerLabel()).toBe("Iteration 2 of 3");
    expect(within(pager()).getByRole("button", { name: "Next iteration" })).toHaveAttribute("aria-disabled", "true");
    expect(within(pager()).getByRole("button", { name: "Previous iteration" })).toHaveAttribute("aria-disabled", "true");
  });

  it("disables 'Failed only' when no iteration failed", async () => {
    await renderView();
    emit(ev("step_started", { step_id: "each", step_type: "foreach" }), ...iteration(0, "completed"));
    expect(within(pager()).getByRole("switch", { name: "Failed only (0)" })).toBeDisabled();
  });
});

describe("loop detail and body step detail", () => {
  it("lists the loop's iterations; a row shows that iteration", async () => {
    await renderView();
    emit(...threeIterations());
    fireEvent.click(within(screen.getByTestId("workflow-step-each")).getAllByRole("button")[0]);
    const table = await screen.findByTestId("workflow-loop-iterations-each");
    expect(within(table).getAllByRole("row")).toHaveLength(4);
    expect(screen.getByTestId("workflow-loop-iteration-each-1")).toHaveAttribute("data-state", "failed");
    expect(screen.getByTestId("workflow-loop-iteration-each-1")).toHaveTextContent("model_error");
    expect(screen.getByTestId("workflow-loop-iteration-each-2")).toHaveAttribute("data-shown", "true");
    fireEvent.click(within(table).getByRole("button", { name: "Show iteration 1" }));
    expect(pagerLabel()).toBe("Iteration 1 of 3");
    expect(screen.getByTestId("workflow-loop-iteration-each-0")).toHaveAttribute("data-shown", "true");
  });

  it("shows a body step's detail from the shown iteration", async () => {
    await renderView();
    emit(...threeIterations());
    fireEvent.click(within(pager()).getByRole("button", { name: "Previous iteration" }));
    fireEvent.click(screen.getByRole("button", { name: /^Step triage, Agent/ }));
    const panel = await screen.findByTestId("step-io-panel");
    expect(within(panel).getByTestId("step-io-iteration")).toHaveTextContent("From each iteration 2");
    expect(within(panel).getByTestId("step-io-error")).toHaveTextContent("model_error");
    fireEvent.click(within(pager()).getByRole("button", { name: "Previous iteration" }));
    expect(within(screen.getByTestId("step-io-panel")).getByTestId("step-io-output")).toHaveTextContent("t0");
  });
});

describe("Timeline iteration groups", () => {
  it("groups iterations under the loop and selects one from its row", async () => {
    await renderView();
    emit(...threeIterations());
    fireEvent.click(screen.getByTestId("workflow-timeline-toggle"));
    expect(screen.getByTestId("workflow-timeline-iteration-each-2")).toHaveAttribute("data-shown", "true");
    expect(screen.getByTestId("workflow-timeline-summary")).toHaveTextContent("of 5 steps done");
    fireEvent.click(within(screen.getByTestId("workflow-timeline-iteration-each-0")).getByRole("button"));
    expect(pagerLabel()).toBe("Iteration 1 of 3");
    expect(screen.getByTestId("workflow-timeline-row-triage")).toHaveAttribute("data-state", "completed");
  });

  it("renders 100 iterations as group rows without flattening the body", async () => {
    await renderView();
    emit(ev("step_started", { step_id: "each", step_type: "foreach" }), ...Array.from({ length: 100 }, (_, i) => iteration(i, "completed")).flat());
    fireEvent.click(screen.getByTestId("workflow-timeline-toggle"));
    const list = screen.getByTestId("workflow-timeline");
    expect(list.querySelectorAll('[data-kind="iteration"]')).toHaveLength(100);
    expect(list.querySelectorAll('[data-testid="workflow-timeline-row-triage"]')).toHaveLength(1);
    expect(pagerLabel()).toBe("Iteration 100 of 100");
  });
});

describe("Cancel", () => {
  it("confirms, cancels, then shows Cancelled and hides the button", async () => {
    await renderView();
    emit(...threeIterations());
    fireEvent.click(screen.getByTestId("workflow-run-cancel"));
    const dialog = await screen.findByRole("dialog", { name: "Cancel this run?" });
    fireEvent.click(within(dialog).getByRole("button", { name: "Keep running" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(wfMock.cancelRun).not.toHaveBeenCalled();

    wfMock.cancelRun.mockImplementation(async () => {
      runsMock.getRun.mockResolvedValue({ ...RUN, status: "failed", display_status: "failed" });
      runsMock.listEvents.mockResolvedValue([
        ev("cancel_requested", { requested_by: "ada" }, "2026-11-15T10:00:09Z"),
        ev("run_failed", { status: "failed", error: "the run was cancelled", error_code: "run_cancelled" }, "2026-11-15T10:00:10Z"),
      ]);
      return { run_id: "run-1", kind: "workflow", status: "failed", display_status: "failed", error_code: "run_cancelled", cancelled: true, requested_by: "ada", approvals_closed: 0 };
    });
    fireEvent.click(screen.getByTestId("workflow-run-cancel"));
    fireEvent.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "Cancel run" }));
    await waitFor(() => expect(screen.getByTestId("workflow-run-status")).toHaveAttribute("data-cancelled", "true"));
    expect(wfMock.cancelRun).toHaveBeenCalledWith("run-1");
    expect(screen.getAllByTestId("workflow-run-cancelled")[0]).toHaveTextContent("Cancelled");
    expect(screen.getByTestId("workflow-run-cancelled-notice")).toHaveTextContent("Cancelled by ada");
    expect(screen.queryByTestId("workflow-run-failed")).not.toBeInTheDocument();
    expect(screen.queryByTestId("workflow-run-cancel")).not.toBeInTheDocument();
    expect(stepState("each")).toBe("failed");
  });

  it("reads 'Cancelling…' once a cancel is recorded, and keeps a 503 in the dialog", async () => {
    await renderView();
    emit(ev("cancel_requested", { requested_by: "ada" }));
    expect(screen.getByTestId("workflow-run-cancel")).toHaveTextContent("Cancelling…");
    wfMock.cancelRun.mockRejectedValue(new ApiError(503, "cancel recorded but not finished"));
    fireEvent.click(screen.getByTestId("workflow-run-cancel"));
    // A recorded cancel: the dialog explains why the button is still clickable.
    const dialog = await screen.findByRole("dialog", { name: "Finish cancelling this run?" });
    expect(dialog).toHaveTextContent("already requested");
    fireEvent.click(within(dialog).getByRole("button", { name: "Finish cancelling" }));
    expect(await screen.findByTestId("confirm-dialog-error")).toHaveTextContent("completes automatically");
  });

  it("closes on a 409 (the run ended meanwhile) and re-reads the run", async () => {
    await renderView();
    wfMock.cancelRun.mockRejectedValue(new ApiError(409, "the run has already ended"));
    const reads = runsMock.getRun.mock.calls.length;
    fireEvent.click(screen.getByTestId("workflow-run-cancel"));
    fireEvent.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "Cancel run" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(screen.queryByTestId("confirm-dialog-error")).not.toBeInTheDocument();
    await waitFor(() => expect(runsMock.getRun.mock.calls.length).toBeGreaterThan(reads));
  });

  it("is not offered for a terminal run", async () => {
    await renderView();
    emit(ev("run_completed", { output: null }));
    expect(screen.queryByTestId("workflow-run-cancel")).not.toBeInTheDocument();
  });

  it("announces Cancelled outside the closed Sheet at 375 px", async () => {
    restoreWidth = setWindowWidth(375);
    render(<WorkflowRunView runId="run-1" workflowId="wf-1" />);
    await screen.findByTestId("workflow-graph");
    emit(ev("run_failed", { status: "failed", error: "cancelled", error_code: "run_cancelled" }));
    expect(screen.getByTestId("workflow-run-status-alert")).toHaveTextContent("Cancelled: open the run details.");
  });
});

describe("a11y", () => {
  it("has no axe violations with the pager, the iteration table and Cancel", async () => {
    await renderView();
    emit(...threeIterations());
    fireEvent.click(within(screen.getByTestId("workflow-step-each")).getAllByRole("button")[0]);
    await screen.findByTestId("workflow-loop-iterations-each");
    fireEvent.click(screen.getByTestId("workflow-timeline-toggle"));
    await expectNoAxeViolations(document.body);
  }, AXE_TIMEOUT_MS);
});

describe("LP.12 review fixes", () => {
  it("a cancel that ended needs_attention keeps the warning first, with 'cancelled' as secondary text", async () => {
    await renderView();
    emit(
      ev("cancel_requested", { requested_by: "ada" }),
      ev("run_failed", { status: "needs_attention", error: "the run was cancelled", error_code: "run_cancelled" }),
    );
    const status = screen.getByTestId("workflow-run-status");
    expect(status).toHaveAttribute("data-status", "needs_attention");
    expect(within(status).getByTestId("status-pill")).toHaveAttribute("data-status", "needs_attention");
    expect(within(status).getByTestId("workflow-run-cancelled-secondary")).toHaveTextContent("cancelled");
    expect(screen.queryByTestId("workflow-run-cancelled")).not.toBeInTheDocument();
    expect(screen.getByTestId("workflow-run-needs-attention")).toHaveTextContent("Needs attention");
    expect(screen.getByTestId("workflow-run-needs-attention-cancelled")).toHaveTextContent("cancelled by ada");
    expect(screen.queryByTestId("workflow-run-cancelled-notice")).not.toBeInTheDocument();
  });

  it("at 375 px a needs_attention cancel keeps the warning banner", async () => {
    restoreWidth = setWindowWidth(375);
    render(<WorkflowRunView runId="run-1" workflowId="wf-1" />);
    await screen.findByTestId("workflow-graph");
    emit(ev("run_failed", { status: "needs_attention", error: "cancelled", error_code: "run_cancelled" }));
    const alert = screen.getByTestId("workflow-run-status-alert");
    expect(alert).toHaveTextContent("Needs attention (cancelled): open the run details before running again.");
    expect(alert.className).toContain("border-warning");
  });

  it("keeps focus on Previous when it reaches the first iteration, and on Latest", async () => {
    await renderView();
    emit(...threeIterations());
    const prev = within(pager()).getByRole("button", { name: "Previous iteration" });
    prev.focus();
    fireEvent.click(prev);
    fireEvent.click(prev);
    expect(pagerLabel()).toBe("Iteration 1 of 3");
    expect(prev).toHaveAttribute("aria-disabled", "true");
    expect(prev).not.toBeDisabled();
    expect(prev).toHaveFocus();
    fireEvent.click(prev);
    expect(pagerLabel()).toBe("Iteration 1 of 3");
    const latest = within(pager()).getByTestId("workflow-iteration-pager-latest");
    latest.focus();
    fireEvent.click(latest);
    expect(pagerLabel()).toBe("Iteration 3 of 3");
    expect(latest).toHaveFocus();
  });

  it("announces only moves the user makes, never a live loop's new iterations", async () => {
    await renderView();
    emit(...threeIterations());
    const announcement = () => within(pager()).getByTestId("workflow-iteration-pager-announcement");
    expect(within(pager()).getByTestId("workflow-iteration-pager-label")).not.toHaveAttribute("aria-live");
    expect(announcement()).toHaveTextContent("");
    emit(...iteration(3, "running"));
    expect(announcement()).toHaveTextContent("");
    fireEvent.click(within(pager()).getByRole("button", { name: "Previous iteration" }));
    expect(announcement()).toHaveTextContent("Iteration 3 of 4");
  });

  it("keys a nested pager's choice by the outer iteration", async () => {
    wfMock.getWorkflowVersion.mockResolvedValue({ workflow_id: "wf-1", version: 1, spec: nestedSpec, spec_hash: "h", created_at: "", created_by: "u" });
    render(<WorkflowRunView runId="run-1" workflowId="wf-1" />);
    await screen.findByTestId("workflow-step-leaf");
    const sc = (...frames: [string, number][]) => frames.map(([loop, index]) => ({ loop, index }));
    emit(
      ev("loop_iteration_started", { step_id: "outer", scope: [], index: 0 }),
      ...[0, 1, 2].map((i) => ev("loop_iteration_started", { step_id: "inner", scope: sc(["outer", 0]), index: i })),
      ev("loop_iteration_started", { step_id: "outer", scope: [], index: 1 }),
      ...[0, 1, 2].map((i) => ev("loop_iteration_started", { step_id: "inner", scope: sc(["outer", 1]), index: i })),
    );
    const inner = () => screen.getByTestId("workflow-iteration-pager-inner");
    fireEvent.click(within(inner()).getByRole("button", { name: "Previous iteration" }));
    expect(within(inner()).getByTestId("workflow-iteration-pager-label")).toHaveTextContent("Iteration 2 of 3");
    // Outer 1 → its inner pager follows its own latest: the choice made in outer 2 does not carry.
    fireEvent.click(within(pager_("outer")).getByRole("button", { name: "Previous iteration" }));
    expect(within(inner()).getByTestId("workflow-iteration-pager-label")).toHaveTextContent("Iteration 3 of 3");
    expect(inner()).toHaveAttribute("data-following", "true");
    // Back to outer 2: its inner choice is still there.
    fireEvent.click(within(pager_("outer")).getByRole("button", { name: "Next iteration" }));
    expect(within(inner()).getByTestId("workflow-iteration-pager-label")).toHaveTextContent("Iteration 2 of 3");
  });
});

function pager_(loopId: string) {
  return screen.getByTestId(`workflow-iteration-pager-${loopId}`);
}
