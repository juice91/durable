// AC.14 tester: waiting pill in the rail, StatusPill, Events tab rows, unresolved refs, 375 px, axe.
import React from "react";
import "@testing-library/jest-dom";
import { render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@/lib/conversations", () => ({ listConversations: vi.fn(), renameConversation: vi.fn() }));
vi.mock("@/lib/clipboard", () => ({ copyText: vi.fn(async () => true) }));

import { listConversations, type Conversation } from "@/lib/conversations";
import type { RunEvent, RunEventData, RunStatus } from "@/lib/runs";
import { deriveRunView } from "@/lib/workflowRunState";
import { ConversationRail, turnUnfinished } from "./ConversationRail";
import { EventTimeline } from "@/components/runs/EventTimeline";
import { StatusPill } from "@/components/runs/StatusPill";
import { StepIoPanel } from "@/components/workflows/StepIoPanel";
import { StructuredOutputView } from "./StructuredOutput";
import { ApprovalCard } from "@/components/approvals/ApprovalCard";
import { expectNoAxeViolations, AXE_TIMEOUT_MS } from "@/test/a11y";

const ev = (seq: number, type: RunEvent["type"], data: unknown): RunEvent => ({ seq, type, data: data as RunEventData, ts: "2026-11-15T00:00:00Z" });
const conv = (extra: Partial<Conversation>): Conversation => ({
  id: "c1", agent_id: "agent-1", title: "Chat", turn_count: 1, last_turn_run_id: "r", last_turn_status: "running",
  last_turn_display_status: "waiting_approval", created_at: "2026-11-15T00:00:00Z", updated_at: "2026-11-15T00:00:00Z", ...extra,
});

beforeEach(() => vi.clearAllMocks());

describe("waiting_approval pill: rail and StatusPill", () => {
  it("the rail labels a waiting conversation and treats it as unfinished", async () => {
    expect(turnUnfinished(conv({}))).toBe(true);
    vi.mocked(listConversations).mockResolvedValue({ items: [conv({})], next_before: null });
    render(<ConversationRail agentId="agent-1" selectedId={null} onSelect={vi.fn()} onNew={vi.fn()} />);
    await waitFor(() => expect(screen.getByText(/Waiting for approval/)).toBeInTheDocument());
  });

  it.each<[RunStatus, string]>([
    ["queued", "Queued"], ["running", "Running"], ["recovering", "Recovering"], ["waiting_approval", "Waiting for approval"],
    ["completed", "Completed"], ["failed", "Failed"], ["needs_attention", "Needs Attention"],
  ])("StatusPill %s reads %s with data-status", (status, label) => {
    render(<StatusPill status={status} />);
    expect(screen.getByTestId("status-pill")).toHaveTextContent(label);
    expect(screen.getByTestId("status-pill")).toHaveAttribute("data-status", status);
  });

  it("StatusPill styles waiting_approval distinctly from running", () => {
    const { rerender } = render(<StatusPill status="waiting_approval" />);
    const waiting = screen.getByTestId("status-pill").className;
    rerender(<StatusPill status="running" />);
    expect(screen.getByTestId("status-pill").className).not.toBe(waiting);
    expect(waiting).toMatch(/warning/);
  });
});

describe("Events tab: every capability event type", () => {
  it("renders output_retry with several paths and the root path", () => {
    render(<EventTimeline currentPartialText={{}} events={[ev(1, "output_retry", { model_request: 0, errors: ["", "a", "b", "c", "d", "e", "f"] })]} />);
    const row = screen.getByTestId("event-output-retry");
    expect(row).toHaveTextContent("(root)");
    expect(row).toHaveTextContent("+2 more");
  });

  it("renders approved and timed-out decisions and the waiting deadline", () => {
    render(
      <EventTimeline currentPartialText={{}} events={[
        ev(1, "tool_approval_waiting", { tool_call_id: "c1", tool_name: "t.a", args: { q: 1 }, deadline: "2099-01-01T00:00:00Z" }),
        ev(2, "tool_approved", { tool_call_id: "c1", decided_by: "dev", comment: "ok go" }),
        ev(3, "tool_approval_timed_out", { tool_call_id: "c2" }),
      ]} />,
    );
    const decisions = screen.getAllByTestId("event-tool-decision");
    expect(decisions.map((d) => d.getAttribute("data-decision"))).toEqual(["tool_approved", "tool_approval_timed_out"]);
    expect(decisions[0]).toHaveTextContent("ok go");
    expect(decisions[1]).toHaveTextContent("timed out");
    expect(screen.getByTestId("event-tool-approval-waiting")).toHaveTextContent("t.a");
  });

  it("shows primary-answered fallback facts and every limit code", () => {
    render(
      <EventTimeline currentPartialText={{}} events={[
        ev(1, "model_message", { model_request: 0, text: "hi", requested_model_id: "test:echo", fallback_index: 0 }),
        ev(2, "run_failed", { status: "failed", error: "x", error_code: "run_budget_exceeded", value: "0.01", spent_usd: "0.02" }),
        ev(3, "run_failed", { status: "failed", error: "x", error_code: "fallback_exhausted", cause: "provider_error" }),
      ]} />,
    );
    expect(screen.getByTestId("event-fallback")).toHaveTextContent("answered by the primary");
    expect(screen.getAllByTestId("event-error-code").map((e) => e.textContent)).toEqual(["run_budget_exceeded", "fallback_exhausted"]);
  });

  it("renders eval-style and unknown events without crashing (JSON fallback)", () => {
    const events = [ev(1, "eval_scored" as RunEvent["type"], { score: 0.9, rubric: "tone" }), ev(2, "eval_failed" as RunEvent["type"], { reason: "judge_error" })];
    render(<EventTimeline currentPartialText={{}} events={events} />);
    expect(screen.getByText("eval_scored")).toBeInTheDocument();
    expect(screen.getByText(/judge_error/)).toBeInTheDocument();
  });
});

describe("workflow step detail: unresolved_refs", () => {
  const stepView = (type: string, refs: unknown) =>
    deriveRunView(null, [ev(1, "step_started", { step_id: "s", step_type: type }), ev(2, "step_completed", { step_id: "s", output: "x", ...(refs === undefined ? {} : { unresolved_refs: refs }) })], "running").steps.s;

  it("lists several paths without the branch sentence for a non-branch step", () => {
    const detail = stepView("agent", ["a.b", "c[0].d"]);
    render(<StepIoPanel step={null} detail={detail} onClose={() => undefined} />);
    const note = screen.getByTestId("step-io-unresolved-refs");
    expect(note).toHaveTextContent("2 references resolved to nothing.");
    expect(note).not.toHaveTextContent("branch");
    expect(within(note).getAllByRole("listitem").map((l) => l.textContent)).toEqual(["a.b", "c[0].d"]);
  });

  it("shows nothing when the key is absent or empty", () => {
    for (const refs of [undefined, []]) {
      const { unmount } = render(<StepIoPanel step={null} detail={stepView("agent", refs)} onClose={() => undefined} />);
      expect(screen.queryByTestId("step-io-unresolved-refs")).toBeNull();
      unmount();
    }
  });

  it("ignores a malformed unresolved_refs value instead of crashing", () => {
    expect(() => render(<StepIoPanel step={null} detail={stepView("agent", "not-a-list")} onClose={() => undefined} />)).not.toThrow();
  });
});

describe("375 px and axe", () => {
  it("long unbroken content stays inside min-w-0 containers and has no axe violations", async () => {
    window.innerWidth = 375;
    const long = "x".repeat(300);
    const { container } = render(
      <div style={{ width: 375 }}>
        <StructuredOutputView value={{ [long]: long, list: [long] }} />
        <ApprovalCard approval={{ kind: "tool", runId: "r", toolCallId: "c1", toolName: "tickets.create", args: { [long]: long }, deadline: "2099-01-01T00:00:00Z" }} onRefresh={() => undefined} />
        <StatusPill status="waiting_approval" />
      </div>,
    );
    const view = screen.getByTestId("structured-output");
    expect(view.className).toContain("min-w-0");
    expect(view.querySelector("dd")!.className).toContain("min-w-0");
    expect(container.querySelector("[class*='w-screen']")).toBeNull();
    await expectNoAxeViolations(container);
  }, AXE_TIMEOUT_MS);
});
