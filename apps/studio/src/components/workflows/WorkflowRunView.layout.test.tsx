import React from "react";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import type { RunEvent, SubscribeHandlers } from "@/lib/runs";
import { demoSpec } from "@/test/workflowFixtures";
import { expectNoAxeViolations, AXE_TIMEOUT_MS } from "@/test/a11y";
import { setWindowWidth } from "@/components/frame/frameTesting";
import { routes } from "@/lib/routes";
import { WORKFLOW_LAYOUT_STORAGE_KEY } from "./workflowLayout";

const runsMock = vi.hoisted(() => ({ getRun: vi.fn(), listEvents: vi.fn(), subscribeRun: vi.fn() }));
const wfMock = vi.hoisted(() => ({ getWorkflowVersion: vi.fn(), decideApproval: vi.fn() }));
const nav = vi.hoisted(() => ({ push: vi.fn(), replace: vi.fn() }));

vi.mock("next/link", () => ({ default: ({ href, children, ...rest }: { href: string; children: React.ReactNode }) => <a href={href} {...rest}>{children}</a> }));
vi.mock("next/navigation", () => ({
  useParams: () => ({ id: "wf-1", runId: "run-1" }),
  useRouter: () => ({ push: nav.push, replace: nav.replace }),
  useSearchParams: () => new URLSearchParams(),
  usePathname: () => "/workflows/wf-1/runs/run-1",
}));
vi.mock("@/lib/runs", async (importOriginal) => ({ ...(await importOriginal<object>()), ...runsMock }));
vi.mock("@/lib/workflows", async (importOriginal) => ({ ...(await importOriginal<object>()), ...wfMock }));

import { WorkflowRunView } from "./WorkflowRunView";
import WorkflowRunPage from "@/app/workflows/[id]/runs/[runId]/page";

let handlers: SubscribeHandlers;
let seq = 0;
const ev = (type: RunEvent["type"], data: Record<string, unknown> = {}, ts = "2026-11-13T10:00:00Z"): RunEvent => ({ seq: seq++, type, data, ts });
const emit = (...events: RunEvent[]) => act(() => events.forEach((event) => handlers.onEvent?.(event)));

const RUN = {
  run_id: "run-1", kind: "workflow", workflow_id: "wf-1", workflow_version: 1, agent_id: null, version: null,
  status: "running", display_status: "running", input: "{}", output: null, error: null, heartbeat_at: null,
  created_at: "2026-11-13T10:00:00Z", updated_at: "2026-11-13T10:00:05Z", trace_id: null, conversation_id: null, turn: null,
};

let restoreWidth: () => void = () => undefined;

beforeAll(() => {
  class RO { observe() {} unobserve() {} disconnect() {} }
  vi.stubGlobal("ResizeObserver", RO);
  vi.stubGlobal("DOMMatrixReadOnly", class { m22 = 1; });
});

beforeEach(() => {
  seq = 0;
  localStorage.clear();
  nav.push.mockReset();
  runsMock.getRun.mockResolvedValue(RUN);
  runsMock.listEvents.mockResolvedValue([]);
  runsMock.subscribeRun.mockImplementation((_id: string, h: SubscribeHandlers) => {
    handlers = h;
    return () => undefined;
  });
  wfMock.getWorkflowVersion.mockResolvedValue({ workflow_id: "wf-1", version: 1, spec: demoSpec, spec_hash: "h", created_at: "", created_by: "u" });
  wfMock.decideApproval.mockReset();
});

afterEach(() => {
  cleanup();
  restoreWidth();
  restoreWidth = () => undefined;
});

async function renderView() {
  render(<WorkflowRunView runId="run-1" workflowId="wf-1" />);
  await screen.findByTestId("workflow-step-classify");
}

describe("WorkflowRunView layout (SL.14)", () => {
  it("shows the run's status, id and duration in the floating panel", async () => {
    await renderView();
    const panel = screen.getByRole("region", { name: "Run details" });
    expect(within(panel).getByTestId("workflow-run-id")).toHaveTextContent("run-1");
    expect(within(panel).getByRole("button", { name: "Copy run id" })).toBeInTheDocument();
    expect(within(panel).getByTestId("workflow-run-duration")).toHaveTextContent("in progress");
    expect(within(panel).getByRole("link", { name: "Run details" })).toHaveAttribute("href", routes.run("run-1"));
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent("Run run-1");
  });

  it("lists Timeline rows whose states match the steps, and selects a step from a row", async () => {
    await renderView();
    const toggle = screen.getByRole("button", { name: "Timeline" });
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    expect(screen.getByTestId("workflow-timeline-summary")).toHaveTextContent("0 of 5 steps done");
    fireEvent.click(toggle);
    expect(JSON.parse(localStorage.getItem(WORKFLOW_LAYOUT_STORAGE_KEY)!)).toMatchObject({ timelineOpen: true });

    emit(
      ev("step_started", { step_id: "classify", step_type: "agent" }, "2026-11-13T10:00:00Z"),
      ev("step_completed", { step_id: "classify", output: "billing" }, "2026-11-13T10:00:01.500Z"),
      ev("step_started", { step_id: "lookup", step_type: "tool" }, "2026-11-13T10:00:02Z"),
    );
    const rows = within(screen.getByTestId("workflow-timeline")).getAllByRole("listitem");
    expect(rows.map((row) => row.getAttribute("data-testid"))).toEqual([
      "workflow-timeline-row-classify", "workflow-timeline-row-lookup", "workflow-timeline-row-route", "workflow-timeline-row-approve", "workflow-timeline-row-summarize",
    ]);
    for (const row of rows) {
      const id = row.getAttribute("data-testid")!.replace("workflow-timeline-row-", "");
      expect(row).toHaveAttribute("data-state", screen.getByTestId(`workflow-step-${id}`).getAttribute("data-state"));
    }
    expect(within(rows[0]).getByTestId("workflow-timeline-duration")).toHaveTextContent("1.50 s");
    expect(within(rows[1]).getByTestId("workflow-timeline-duration")).toHaveTextContent("in progress");
    expect(screen.getByTestId("workflow-timeline-summary")).toHaveTextContent("1 of 5 steps done");

    fireEvent.click(within(rows[0]).getByRole("button", { pressed: false }));
    expect(screen.getByTestId("step-io-panel")).toHaveAttribute("data-step-id", "classify");

    const handle = screen.getByRole("separator", { name: "Resize timeline" });
    fireEvent.keyDown(handle, { key: "ArrowUp" });
    expect(JSON.parse(localStorage.getItem(WORKFLOW_LAYOUT_STORAGE_KEY)!)).toMatchObject({ timelineHeight: 256 });
  });

  it("approves a pending step from the floating Needs input overlay", async () => {
    await renderView();
    emit(ev("step_started", { step_id: "approve", step_type: "approval" }), ev("step_waiting", { step_id: "approve", prompt: "Escalate?" }));
    const overlay = screen.getByTestId("workflow-approval-overlay");
    expect(within(overlay).getByTestId("workflow-needs-input")).toHaveTextContent("Needs input");
    wfMock.decideApproval.mockImplementation(async () => {
      runsMock.listEvents.mockResolvedValue([
        ev("step_started", { step_id: "approve", step_type: "approval" }, "2026-11-13T10:00:00Z"),
        ev("step_waiting", { step_id: "approve", prompt: "Escalate?" }),
        ev("step_approved", { step_id: "approve", decided_by: "ada" }),
        ev("step_completed", { step_id: "approve", output: { approved: true } }),
      ]);
      return { run_id: "run-1", step_id: "approve", decision: "approved", decided_by: "ada", comment: null, notified: true };
    });
    fireEvent.click(within(overlay).getByRole("button", { name: "Approve" }));
    await waitFor(() => expect(wfMock.decideApproval).toHaveBeenCalledWith("run-1", "approve", "approve", ""));
    await waitFor(() => expect(screen.queryByTestId("workflow-approval-overlay")).not.toBeInTheDocument());
    expect(screen.getByTestId("workflow-step-approve")).toHaveAttribute("data-state", "completed");
  });

  it("opens the step detail as a Sheet where the right column does not fit", async () => {
    restoreWidth = setWindowWidth(800);
    await renderView();
    fireEvent.click(screen.getByRole("button", { name: "Step classify, Agent, Pending" }));
    const sheet = await screen.findByRole("dialog", { name: "Step classify" });
    expect(within(sheet).getByTestId("step-io-panel")).toHaveAttribute("data-step-id", "classify");
    fireEvent.keyDown(sheet, { key: "Escape" });
    await waitFor(() => expect(screen.queryByTestId("step-io-panel")).not.toBeInTheDocument());
  });

  it("has no axe violations with an approval waiting and a step open", async () => {
    await renderView();
    emit(ev("step_waiting", { step_id: "approve", prompt: "Escalate?" }));
    fireEvent.click(screen.getByRole("button", { name: "Step classify, Agent, Pending" }));
    await expectNoAxeViolations(document.body);
  }, AXE_TIMEOUT_MS);
});

describe("WorkflowRunView after the SL.14 review", () => {
  it("keeps the Events disclosure open across collapsing and expanding the panel", async () => {
    await renderView();
    const summary = screen.getByText("Show the event list");
    fireEvent.click(summary);
    const details = summary.closest("details")!;
    details.open = true;
    fireEvent.click(screen.getByRole("button", { name: "Hide run details panel" }));
    fireEvent.click(screen.getByRole("button", { name: "Show run details panel" }));
    expect(screen.getByText("Show the event list").closest("details")).toBe(details);
    expect(details.open).toBe(true);
  });

  it("moves focus to the run status after Approve removes the overlay", async () => {
    await renderView();
    emit(ev("step_started", { step_id: "approve", step_type: "approval" }), ev("step_waiting", { step_id: "approve", prompt: "?" }));
    wfMock.decideApproval.mockImplementation(async () => {
      runsMock.listEvents.mockResolvedValue([
        ev("step_started", { step_id: "approve", step_type: "approval" }),
        ev("step_waiting", { step_id: "approve", prompt: "?" }),
        ev("step_approved", { step_id: "approve", decided_by: "ada" }),
      ]);
      return { run_id: "run-1", step_id: "approve", decision: "approved", decided_by: "ada", comment: null, notified: true };
    });
    const approve = within(screen.getByTestId("workflow-approval-overlay")).getByRole("button", { name: "Approve" });
    approve.focus();
    fireEvent.click(approve);
    await waitFor(() => expect(screen.queryByTestId("workflow-approval-overlay")).not.toBeInTheDocument());
    await waitFor(() => expect(screen.getByTestId("workflow-run-status")).toHaveFocus());
  });

  it("announces a failed run outside the closed bottom Sheet below 768 px", async () => {
    restoreWidth = setWindowWidth(375);
    render(<WorkflowRunView runId="run-1" workflowId="wf-1" />);
    // At 375 px the step list is in the closed bottom Sheet; the canvas is on the page.
    await screen.findByTestId("workflow-graph");
    expect(screen.queryByTestId("workflow-step-list")).not.toBeInTheDocument();
    act(() => handlers.onEvent?.(ev("run_failed", { status: "failed", error: "boom", step_id: "lookup" })));
    expect(screen.getByTestId("workflow-run-status-alert")).toHaveTextContent("Failed at step lookup");
    expect(screen.queryByTestId("workflow-run-failed")).not.toBeInTheDocument();
  });
});

describe("workflow run page (SL.14)", () => {
  it("hosts the trace Sheet: Run details opens it in place on traceTab", async () => {
    render(<WorkflowRunPage />);
    await screen.findByTestId("workflow-step-classify");
    expect(within(screen.getByRole("navigation", { name: "Workflow sections" })).getByRole("link", { name: "Graph" })).toHaveAttribute("aria-current", "page");
    fireEvent.click(screen.getByRole("link", { name: "Run details" }));
    expect(nav.push).toHaveBeenCalledTimes(1);
    const target = new URL(String(nav.push.mock.calls[0][0]), "http://studio.test");
    expect(target.pathname).toBe(routes.workflowRun("wf-1", "run-1"));
    expect(Object.fromEntries(target.searchParams)).toEqual({ trace: "run-1", traceTab: "events" });
  });
});
