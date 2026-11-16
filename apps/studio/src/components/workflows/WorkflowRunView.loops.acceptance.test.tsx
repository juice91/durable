// LP.12 acceptance: pager, iteration table, step detail, Cancel and layout in the run view.
import React from "react";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import type { RunEvent, SubscribeHandlers } from "@/lib/runs";
import { ApiError } from "@/lib/api";
import { foreachSpec, nestedSpec, untilSpec } from "@/test/workflowLoopFixtures";
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
let restoreWidth: () => void = () => undefined;
const ev = (type: string, data: Record<string, unknown> = {}): RunEvent => ({ seq: seq++, type: type as RunEvent["type"], data, ts: "2026-11-15T10:00:00Z" });
const emit = (...events: RunEvent[]) => act(() => events.forEach((event) => handlers.onEvent?.(event)));
const sc = (...frames: [string, number][]) => frames.map(([loop, index]) => ({ loop, index }));

const RUN = {
  run_id: "run-1", kind: "workflow", workflow_id: "wf-1", workflow_version: 1, agent_id: null, version: null,
  status: "running", display_status: "running", input: "{}", output: null, error: null, heartbeat_at: null,
  created_at: "2026-11-15T10:00:00Z", updated_at: "2026-11-15T10:00:05Z", trace_id: null, conversation_id: null, turn: null,
};

/** foreach `each`: iterations 0..n-1, the ones in `failed` fail (code model_error), the last stays running if `open`. */
function each(n: number, failed: number[] = [], open = false): RunEvent[] {
  const out: RunEvent[] = [ev("step_started", { step_id: "each", step_type: "foreach" })];
  for (let i = 0; i < n; i++) {
    const scope = sc(["each", i]);
    out.push(ev("loop_iteration_started", { step_id: "each", scope: [], index: i }), ev("step_started", { step_id: "triage", step_type: "agent", scope }));
    if (open && i === n - 1) break;
    if (failed.includes(i)) {
      out.push(ev("step_failed", { step_id: "triage", scope, status: "failed", error: "boom", error_code: "model_error" }));
      out.push(ev("loop_iteration_failed", { step_id: "each", scope: [], index: i, status: "failed", error: "boom", error_code: "model_error", failed_step_id: "triage" }));
    } else {
      out.push(ev("step_completed", { step_id: "triage", scope, output: `t${i}` }), ev("loop_iteration_completed", { step_id: "each", scope: [], index: i, state: null }));
    }
  }
  return out;
}

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
  runsMock.subscribeRun.mockImplementation((_id: string, h: SubscribeHandlers) => { handlers = h; return () => undefined; });
  wfMock.getWorkflowVersion.mockResolvedValue({ workflow_id: "wf-1", version: 1, spec: foreachSpec, spec_hash: "h", created_at: "", created_by: "u" });
  wfMock.cancelRun.mockReset();
});
afterEach(() => { cleanup(); restoreWidth(); restoreWidth = () => undefined; });

async function renderView(spec = foreachSpec, waitFor_ = "workflow-step-triage") {
  wfMock.getWorkflowVersion.mockResolvedValue({ workflow_id: "wf-1", version: 1, spec, spec_hash: "h", created_at: "", created_by: "u" });
  render(<WorkflowRunView runId="run-1" workflowId="wf-1" />);
  await screen.findByTestId(waitFor_);
}
const pager = (id = "each") => screen.getByTestId(`workflow-iteration-pager-${id}`);
const label = (id = "each") => within(pager(id)).getByTestId("workflow-iteration-pager-label").textContent;
const btn = (name: string, id = "each") => within(pager(id)).getByRole("button", { name });
const stepState = (id: string) => screen.getByTestId(`workflow-step-${id}`).getAttribute("data-state");

describe("pager", () => {
  it("steps with the buttons and both arrow keys, labels 'Iteration N of M', disables at the ends", async () => {
    await renderView();
    emit(...each(4));
    expect(label()).toBe("Iteration 4 of 4");
    expect(btn("Next iteration")).toHaveAttribute("aria-disabled", "true");
    fireEvent.keyDown(pager(), { key: "ArrowLeft" });
    expect(label()).toBe("Iteration 3 of 4");
    fireEvent.keyDown(btn("Previous iteration"), { key: "ArrowLeft" });
    fireEvent.keyDown(btn("Previous iteration"), { key: "ArrowLeft" });
    expect(label()).toBe("Iteration 1 of 4");
    expect(btn("Previous iteration")).toHaveAttribute("aria-disabled", "true");
    fireEvent.keyDown(pager(), { key: "ArrowLeft" });
    expect(label()).toBe("Iteration 1 of 4");
    fireEvent.keyDown(pager(), { key: "ArrowRight" });
    expect(label()).toBe("Iteration 2 of 4");
    fireEvent.click(btn("Next iteration"));
    expect(label()).toBe("Iteration 3 of 4");
    fireEvent.keyDown(pager(), { key: "Home" });
    expect(label()).toBe("Iteration 3 of 4");
  });

  it("says so before any iteration exists and offers no pager state", async () => {
    await renderView();
    emit(ev("step_started", { step_id: "each", step_type: "foreach" }));
    expect(label()).toBe("No iterations yet");
    expect(btn("Previous iteration")).toHaveAttribute("aria-disabled", "true");
    expect(btn("Next iteration")).toHaveAttribute("aria-disabled", "true");
    expect(within(pager()).queryByTestId("workflow-iteration-pager-state")).not.toBeInTheDocument();
  });

  it("follows the latest until the user picks, and Latest (or picking the newest) resumes following", async () => {
    await renderView();
    emit(...each(2));
    expect(pager()).toHaveAttribute("data-following", "true");
    emit(ev("loop_iteration_started", { step_id: "each", scope: [], index: 2 }));
    expect(label()).toBe("Iteration 3 of 3");
    fireEvent.click(btn("Previous iteration"));
    expect(pager()).toHaveAttribute("data-following", "false");
    emit(ev("loop_iteration_started", { step_id: "each", scope: [], index: 3 }));
    expect(label()).toBe("Iteration 2 of 4");
    fireEvent.click(within(pager()).getByTestId("workflow-iteration-pager-latest"));
    expect(label()).toBe("Iteration 4 of 4");
    expect(pager()).toHaveAttribute("data-following", "true");
    emit(ev("loop_iteration_started", { step_id: "each", scope: [], index: 4 }));
    expect(label()).toBe("Iteration 5 of 5");
  });

  it("'Failed only' is disabled at 0, jumps to the first failure from a passing one, and pages only failures", async () => {
    await renderView();
    emit(...each(5, [1, 3]));
    const sw = () => within(pager()).getByRole("switch", { name: "Failed only (2)" });
    expect(label()).toBe("Iteration 5 of 5");
    expect(sw()).not.toBeDisabled();
    fireEvent.click(sw());
    expect(label()).toBe("Iteration 2 of 5");
    expect(btn("Previous iteration")).toHaveAttribute("aria-disabled", "true");
    fireEvent.click(btn("Next iteration"));
    expect(label()).toBe("Iteration 4 of 5");
    expect(btn("Next iteration")).toHaveAttribute("aria-disabled", "true");
    fireEvent.click(sw());
    fireEvent.click(btn("Previous iteration"));
    expect(label()).toBe("Iteration 3 of 5");
  });

  it("'Failed only' stays disabled when nothing failed", async () => {
    await renderView();
    emit(...each(2));
    const sw = within(pager()).getByRole("switch", { name: "Failed only (0)" });
    expect(sw).toBeDisabled();
    fireEvent.click(sw);
    expect(label()).toBe("Iteration 2 of 2");
  });

  it("resolves a nested inner pager through the outer loop's shown iteration", async () => {
    await renderView(nestedSpec, "workflow-step-leaf");
    emit(
      ev("loop_iteration_started", { step_id: "outer", scope: [], index: 0 }),
      ev("loop_iteration_started", { step_id: "inner", scope: sc(["outer", 0]), index: 0 }),
      ev("loop_iteration_completed", { step_id: "inner", scope: sc(["outer", 0]), index: 0, state: {} }),
      ev("loop_iteration_started", { step_id: "outer", scope: [], index: 1 }),
      ev("loop_iteration_started", { step_id: "inner", scope: sc(["outer", 1]), index: 0 }),
      ev("loop_iteration_started", { step_id: "inner", scope: sc(["outer", 1]), index: 1 }),
      ev("loop_iteration_started", { step_id: "inner", scope: sc(["outer", 1]), index: 2 }),
    );
    expect(label("outer")).toBe("Iteration 2 of 2");
    expect(label("inner")).toBe("Iteration 3 of 3");
    expect(pager("inner")).toHaveAttribute("data-depth", "1");
    fireEvent.click(btn("Previous iteration", "outer"));
    expect(label("outer")).toBe("Iteration 1 of 2");
    expect(label("inner")).toBe("Iteration 1 of 1");
    expect(btn("Next iteration", "inner")).toHaveAttribute("aria-disabled", "true");
  });
});

describe("iteration table and step detail", () => {
  it("lists rows with a Show button and a Note column: error code, until met, until unknown", async () => {
    await renderView(untilSpec, "workflow-step-check");
    emit(
      ev("step_started", { step_id: "poll", step_type: "loop" }),
      ev("loop_iteration_started", { step_id: "poll", scope: [], index: 0, state: { n: 0 } }),
      ev("loop_iteration_completed", { step_id: "poll", scope: [], index: 0, state: { n: 1 }, until: "unknown" }),
      ev("loop_iteration_started", { step_id: "poll", scope: [], index: 1, state: { n: 1 } }),
      ev("loop_iteration_failed", { step_id: "poll", scope: [], index: 1, status: "failed", error: "x", error_code: "tool_error" }),
      ev("loop_iteration_started", { step_id: "poll", scope: [], index: 2, state: { n: 1 } }),
      ev("loop_iteration_completed", { step_id: "poll", scope: [], index: 2, state: { n: 2 }, until: "true" }),
    );
    fireEvent.click(within(screen.getByTestId("workflow-step-poll")).getAllByRole("button")[0]);
    const table = await screen.findByTestId("workflow-loop-iterations-poll");
    expect(within(table).getByRole("columnheader", { name: "Note" })).toBeInTheDocument();
    expect(screen.getByTestId("workflow-loop-iteration-poll-0")).toHaveTextContent("until unknown");
    expect(screen.getByTestId("workflow-loop-iteration-poll-1")).toHaveTextContent("tool_error");
    expect(screen.getByTestId("workflow-loop-iteration-poll-2")).toHaveTextContent("until met");
    expect(screen.getByTestId("workflow-loop-iteration-poll-2")).toHaveAttribute("data-shown", "true");
    fireEvent.click(within(table).getByRole("button", { name: "Show iteration 2" }));
    expect(label("poll")).toBe("Iteration 2 of 3");
    expect(screen.getByTestId("workflow-loop-iteration-poll-1")).toHaveAttribute("data-shown", "true");
    expect(screen.getByTestId("workflow-loop-iteration-poll-2")).toHaveAttribute("data-shown", "false");
    expect(table.parentElement?.className).toContain("max-h-64");
  });

  it("shows 'From each iteration 2' for a body step", async () => {
    await renderView();
    emit(...each(3, [1]));
    fireEvent.click(btn("Previous iteration"));
    fireEvent.click(screen.getByRole("button", { name: /^Step triage, Agent/ }));
    expect(await screen.findByTestId("step-io-iteration")).toHaveTextContent("From each iteration 2");
  });

  it("shows the nested origin 'outer iteration 3 › inner iteration 1'", async () => {
    await renderView(nestedSpec, "workflow-step-leaf");
    emit(
      ...[0, 1, 2].map((i) => ev("loop_iteration_started", { step_id: "outer", scope: [], index: i })),
      ev("loop_iteration_started", { step_id: "inner", scope: sc(["outer", 2]), index: 0 }),
      ev("step_completed", { step_id: "leaf", scope: sc(["outer", 2], ["inner", 0]), output: 7 }),
    );
    fireEvent.click(screen.getByRole("button", { name: /^Step leaf/ }));
    expect(await screen.findByTestId("step-io-iteration")).toHaveTextContent("outer iteration 3 › inner iteration 1");
    expect(screen.getByTestId("step-io-output")).toHaveTextContent("7");
  });
});

describe("Cancel", () => {
  const confirmCancel = async () => {
    fireEvent.click(screen.getByTestId("workflow-run-cancel"));
    fireEvent.click(within(await screen.findByRole("dialog", { name: "Cancel this run?" })).getByRole("button", { name: "Cancel run" }));
  };
  const cancelled = (by = "ada") => {
    runsMock.getRun.mockResolvedValue({ ...RUN, status: "failed", display_status: "failed" });
    runsMock.listEvents.mockResolvedValue([
      ev("cancel_requested", { requested_by: by }),
      ev("run_failed", { status: "failed", error: "cancelled", error_code: "run_cancelled" }),
    ]);
  };

  it("a 503 keeps the message in the dialog and the retry succeeds", async () => {
    await renderView();
    wfMock.cancelRun.mockRejectedValueOnce(new ApiError(503, "recorded"));
    await confirmCancel();
    expect(await screen.findByTestId("confirm-dialog-error")).toHaveTextContent("has not finished yet");
    expect(screen.getByRole("dialog")).toBeInTheDocument();
    wfMock.cancelRun.mockImplementationOnce(async () => {
      cancelled();
      return { run_id: "run-1", kind: "workflow", status: "failed", display_status: "failed", error_code: "run_cancelled", cancelled: true, requested_by: "ada", approvals_closed: 0 };
    });
    fireEvent.click(within(screen.getByRole("dialog")).getByRole("button", { name: "Cancel run" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(wfMock.cancelRun).toHaveBeenCalledTimes(2);
    await waitFor(() => expect(screen.getByTestId("workflow-run-cancelled-notice")).toHaveTextContent("Cancelled by ada"));
  });

  it("a 409 closes the dialog without an error and re-reads", async () => {
    await renderView();
    wfMock.cancelRun.mockRejectedValue(new ApiError(409, "ended"));
    const reads = runsMock.getRun.mock.calls.length;
    await confirmCancel();
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    await waitFor(() => expect(runsMock.getRun.mock.calls.length).toBeGreaterThan(reads));
  });

  it("reads 'Cancelling…' after cancel_requested and the run is not yet cancelled", async () => {
    await renderView();
    emit(ev("cancel_requested", { requested_by: "ada" }));
    expect(screen.getByTestId("workflow-run-cancel")).toHaveTextContent("Cancelling…");
    expect(screen.getByTestId("workflow-run-status")).toHaveAttribute("data-cancelled", "false");
    expect(screen.queryByTestId("workflow-run-cancelled")).not.toBeInTheDocument();
  });

  it("an event-driven cancel shows the pill and notice instead of Failed, and hides the button", async () => {
    await renderView();
    emit(...each(2, [], true), ev("cancel_requested", { requested_by: "bob" }), ev("run_failed", { status: "failed", error: "c", error_code: "run_cancelled" }));
    expect(screen.getAllByTestId("workflow-run-cancelled")[0]).toHaveTextContent("Cancelled");
    expect(screen.getByTestId("workflow-run-cancelled-notice")).toHaveTextContent("Cancelled by bob");
    expect(screen.queryByTestId("workflow-run-failed")).not.toBeInTheDocument();
    expect(screen.queryByTestId("workflow-run-cancel")).not.toBeInTheDocument();
  });

  it.each(["completed", "failed", "needs_attention"])("is not offered for a %s run", async (status) => {
    runsMock.getRun.mockResolvedValue({ ...RUN, status, display_status: status });
    await renderView();
    await waitFor(() => expect(screen.getByTestId("workflow-run-status")).toHaveAttribute("data-status", status));
    expect(screen.queryByTestId("workflow-run-cancel")).not.toBeInTheDocument();
  });

  it("is not offered for a non-workflow run", async () => {
    runsMock.getRun.mockResolvedValue({ ...RUN, kind: "agent" });
    render(<WorkflowRunView runId="run-1" workflowId="wf-1" />);
    await screen.findByTestId("workflow-run-status");
    expect(screen.queryByTestId("workflow-run-cancel")).not.toBeInTheDocument();
  });
});

describe("layout, keyboard and a11y", () => {
  it("at 375 px announces Cancelled in the alert, and a plain failure keeps the failed alert", async () => {
    restoreWidth = setWindowWidth(375);
    render(<WorkflowRunView runId="run-1" workflowId="wf-1" />);
    await screen.findByTestId("workflow-graph");
    emit(ev("cancel_requested", { requested_by: "ada" }), ev("run_failed", { status: "failed", error: "c", error_code: "run_cancelled" }));
    const alert = screen.getByTestId("workflow-run-status-alert");
    expect(alert).toHaveTextContent("Cancelled: open the run details.");
    expect(alert).toHaveAttribute("data-cancelled", "true");
    expect(screen.queryByTestId("workflow-run-cancel")).not.toBeInTheDocument();
  });

  it("at 375 px the Cancel button is reachable and the pager wraps instead of overflowing", async () => {
    restoreWidth = setWindowWidth(375);
    render(<WorkflowRunView runId="run-1" workflowId="wf-1" />);
    await screen.findByTestId("workflow-graph");
    emit(...each(3, [1]));
    expect(screen.getByTestId("workflow-run-cancel")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /details/i }));
    const group = await screen.findByTestId("workflow-iteration-pager-each");
    for (const el of [group, ...Array.from(group.querySelectorAll<HTMLElement>("div"))]) {
      expect(el.className).not.toMatch(/\bw-\[\d{3,}px\]|\bmin-w-\[\d{3,}px\]/);
    }
    expect(group.querySelector(".flex-wrap")).not.toBeNull();
  });

  it("the Cancel dialog closes on Escape and returns focus to the trigger; buttons are keyboard operable", async () => {
    await renderView();
    const trigger = screen.getByTestId("workflow-run-cancel");
    trigger.focus();
    fireEvent.click(trigger);
    const dialog = await screen.findByRole("dialog", { name: "Cancel this run?" });
    fireEvent.keyDown(dialog, { key: "Escape" });
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(wfMock.cancelRun).not.toHaveBeenCalled();
    await waitFor(() => expect(trigger).toHaveFocus());
  });

  it("every pager control is a native focusable control with an accessible name", async () => {
    await renderView();
    emit(...each(3, [1]));
    fireEvent.click(btn("Previous iteration"));
    const group = pager();
    expect(group).toHaveAccessibleName("Iterations of each");
    for (const control of [btn("Previous iteration"), btn("Next iteration"), within(group).getByRole("switch", { name: "Failed only (1)" }), within(group).getByRole("button", { name: "Latest" })]) {
      expect(control).not.toHaveAttribute("tabindex", "-1");
      control.focus();
      expect(control).toHaveFocus();
    }
  });

  it("has no axe violations with the Cancel dialog open", async () => {
    await renderView();
    emit(...each(3, [1]));
    fireEvent.click(screen.getByTestId("workflow-run-cancel"));
    await screen.findByRole("dialog", { name: "Cancel this run?" });
    await expectNoAxeViolations(document.body);
  }, AXE_TIMEOUT_MS);

  it("has no axe violations after a cancel (pill, notice, iterations) at 375 px", async () => {
    restoreWidth = setWindowWidth(375);
    render(<WorkflowRunView runId="run-1" workflowId="wf-1" />);
    await screen.findByTestId("workflow-graph");
    emit(...each(2, [], true), ev("cancel_requested", { requested_by: "ada" }), ev("run_failed", { status: "failed", error: "c", error_code: "run_cancelled" }));
    await expectNoAxeViolations(document.body);
  }, AXE_TIMEOUT_MS);
});
