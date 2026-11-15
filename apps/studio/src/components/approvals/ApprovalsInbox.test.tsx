import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import React from "react";
import "@testing-library/jest-dom";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { nextRouter } from "@/test/nextRouterMock";
import { decideToolApproval, findApproval, listApprovals, refreshPendingApprovals, type Approval } from "@/lib/approvals";
import { getRun } from "@/lib/runs";
import { routes } from "@/lib/routes";
import ApprovalsPage from "@/app/approvals/page";

vi.mock("next/navigation", async () => (await import("@/test/nextRouterMock")).navigationModule);

const RUN = "11111111-2222-3333-4444-555555555555";
const WF_RUN = "22222222-2222-3333-4444-555555555555";
const AGENT = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee";

vi.mock("@/lib/api", async (importOriginal) => {
  const original = await importOriginal<typeof import("@/lib/api")>();
  return { ...original, api: { ...original.api, agents: { ...original.api.agents, list: vi.fn(async () => [{ id: AGENT, name: "Support bot" }]) } } };
});
vi.mock("@/lib/runs", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/runs")>()),
  getRun: vi.fn(),
}));
vi.mock("@/lib/approvals", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/approvals")>()),
  listApprovals: vi.fn(),
  findApproval: vi.fn(),
  decideToolApproval: vi.fn(),
  refreshPendingApprovals: vi.fn(async () => undefined),
  // The Sheet follows a decided item's run quickly here (3 s in the app).
  APPROVAL_FOLLOW_POLL_MS: 20,
}));

const toolItem: Approval = {
  run_id: RUN,
  kind: "tool",
  approval_key: "call_1",
  tool_call_id: "call_1",
  step_id: null,
  root_run_id: RUN,
  agent_id: AGENT,
  workflow_id: null,
  tool_name: "send_email",
  args: { to: "a@example.com" },
  prompt: null,
  status: "pending",
  requested_at: "2026-11-15T10:00:00+00:00",
  deadline: "2099-11-16T10:00:00+00:00",
  decided_at: null,
  decision: null,
  decided_by: null,
  run_status: "running",
};
const stepItem: Approval = {
  ...toolItem,
  run_id: WF_RUN,
  kind: "step",
  approval_key: "review",
  tool_call_id: null,
  step_id: "review",
  root_run_id: WF_RUN,
  agent_id: null,
  workflow_id: "WF1",
  tool_name: null,
  args: null,
  prompt: "Ship it?",
};

describe("Approvals inbox", () => {
  beforeEach(() => {
    nextRouter.reset(["/approvals"]);
    vi.mocked(listApprovals).mockReset();
    vi.mocked(listApprovals).mockResolvedValue({ items: [toolItem, stepItem], next_cursor: null });
    vi.mocked(decideToolApproval).mockReset();
    vi.mocked(findApproval).mockReset();
    vi.mocked(findApproval).mockResolvedValue(null);
    vi.mocked(refreshPendingApprovals).mockClear();
    vi.mocked(getRun).mockReset();
    vi.mocked(getRun).mockResolvedValue({ conversation_id: "CONV1" } as never);
  });
  afterEach(() => vi.useRealTimers());

  it("lists both kinds", async () => {
    render(<ApprovalsPage />);
    expect(await screen.findByTestId(`approval-row-${RUN}:call_1`)).toHaveTextContent("send_email");
    expect(screen.getByTestId(`approval-row-${WF_RUN}:review`)).toHaveTextContent("review");
    expect(vi.mocked(listApprovals).mock.calls[0][0]).toEqual({ status: "pending", kind: null, agent_id: null });
  });

  it("filters by status, kind and agent through the URL", async () => {
    render(<ApprovalsPage />);
    await screen.findByTestId(`approval-row-${RUN}:call_1`);
    fireEvent.change(screen.getByTestId("approvals-kind-filter"), { target: { value: "tool" } });
    await act(() => nextRouter.settle());
    expect(nextRouter.url).toBe(routes.approvals({ kind: "tool" }));
    await waitFor(() => expect(vi.mocked(listApprovals).mock.calls.at(-1)?.[0]).toEqual({ status: "pending", kind: "tool", agent_id: null }));
    await screen.findByRole("option", { name: "Support bot" });
    fireEvent.change(screen.getByTestId("approvals-agent-filter"), { target: { value: AGENT } });
    await act(() => nextRouter.settle());
    expect(nextRouter.url).toBe(routes.approvals({ kind: "tool", agent: AGENT }));
    fireEvent.click(screen.getByRole("radio", { name: "Decided" }));
    await act(() => nextRouter.settle());
    await waitFor(() => expect(vi.mocked(listApprovals).mock.calls.at(-1)?.[0]).toEqual({ status: "decided", kind: "tool", agent_id: AGENT }));
  });

  it("shows the empty state", async () => {
    vi.mocked(listApprovals).mockResolvedValue({ items: [], next_cursor: null });
    render(<ApprovalsPage />);
    expect(await screen.findByText("Nothing is waiting for approval.")).toBeInTheDocument();
  });

  it("opens the Sheet from a row, decides inline, and updates the row in place", async () => {
    vi.mocked(decideToolApproval).mockResolvedValue({ run_id: RUN, tool_call_id: "call_1", decision: "approved", decided_by: "u", comment: null, notified: true });
    render(<ApprovalsPage />);
    const row = await screen.findByTestId(`approval-row-${RUN}:call_1`);
    fireEvent.click(within(row).getByRole("button"));
    await act(() => nextRouter.settle());
    expect(nextRouter.url).toBe(routes.approval(RUN, "call_1"));
    const sheet = await screen.findByTestId("approval-sheet");
    expect(await within(sheet).findByTestId("approval-sheet-open-conversation")).toHaveAttribute("href", routes.agentChat(AGENT, { conversation: "CONV1" }));
    expect(within(sheet).getByTestId("approval-sheet-open-trace")).toHaveAttribute("href", routes.trace(RUN));
    fireEvent.click(within(sheet).getByRole("button", { name: "Approve" }));
    await waitFor(() => expect(decideToolApproval).toHaveBeenCalledWith(RUN, "call_1", "approve", ""));
    await waitFor(() => expect(screen.getByTestId(`approval-row-status-${RUN}:call_1`)).toHaveTextContent("Approved"));
    expect(refreshPendingApprovals).toHaveBeenCalled();
    // The row stays (no reload dropped it from the Pending list).
    expect(listApprovals).toHaveBeenCalledTimes(1);
  });

  it("re-reads a decided item by its own kind, and follows its run until it ends", async () => {
    vi.mocked(decideToolApproval).mockResolvedValue({ run_id: RUN, tool_call_id: "call_1", decision: "approved", decided_by: "u", comment: null, notified: true });
    const decided = { ...toolItem, status: "decided" as const, decision: "approved" as const, decided_by: "u", decided_at: "2026-11-15T10:01:00+00:00" };
    // The first re-reads still see the run going; then it completes.
    vi.mocked(findApproval)
      .mockResolvedValueOnce({ ...decided, run_status: "running" })
      .mockResolvedValueOnce({ ...decided, run_status: "running" })
      .mockResolvedValue({ ...decided, run_status: "completed" });
    nextRouter.reset([routes.approval(RUN, "call_1")]);
    render(<ApprovalsPage />);
    const sheet = await screen.findByTestId("approval-sheet");
    expect(await within(sheet).findByText("running")).toBeInTheDocument();
    fireEvent.click(await within(sheet).findByRole("button", { name: "Approve" }));
    await waitFor(() => expect(within(sheet).getByText("completed")).toBeInTheDocument());
    // Every re-read named the tool kind (no request to the step route for a tool call).
    expect(findApproval).toHaveBeenCalled();
    for (const call of vi.mocked(findApproval).mock.calls) expect(call).toEqual([RUN, "call_1", { kind: "tool" }]);
    // Once the run has ended, the Sheet stops polling.
    const calls = vi.mocked(findApproval).mock.calls.length;
    await new Promise((resolve) => setTimeout(resolve, 120));
    expect(vi.mocked(findApproval).mock.calls.length).toBe(calls);
    expect(within(sheet).getByTestId("approval-sheet-status")).toHaveTextContent("Approved");
  });

  it("follows a decided workflow step by the step kind", async () => {
    const decidedStep = { ...stepItem, status: "decided" as const, decision: "approved" as const, decided_by: "u", decided_at: "2026-11-15T10:01:00+00:00" };
    vi.mocked(listApprovals).mockResolvedValue({ items: [toolItem, { ...decidedStep, run_status: "running" }], next_cursor: null });
    vi.mocked(findApproval).mockResolvedValue({ ...decidedStep, run_status: "completed" });
    nextRouter.reset([routes.approval(WF_RUN, "review")]);
    render(<ApprovalsPage />);
    const sheet = await screen.findByTestId("approval-sheet");
    await waitFor(() => expect(within(sheet).getByText("completed")).toBeInTheDocument());
    // The row is on the loaded page, so the only reads are the follow poll's, by the step kind.
    expect(findApproval).toHaveBeenCalledWith(WF_RUN, "review", { kind: "step" });
    for (const call of vi.mocked(findApproval).mock.calls) expect(call).toEqual([WF_RUN, "review", { kind: "step" }]);
    const calls = vi.mocked(findApproval).mock.calls.length;
    await new Promise((resolve) => setTimeout(resolve, 120));
    expect(vi.mocked(findApproval).mock.calls.length).toBe(calls);
  });

  it("does not follow a decided item without a run status", async () => {
    const decided = { ...toolItem, status: "decided" as const, decision: "approved" as const, decided_by: "u", decided_at: "2026-11-15T10:01:00+00:00", run_status: null };
    vi.mocked(listApprovals).mockResolvedValue({ items: [decided], next_cursor: null });
    nextRouter.reset([routes.approval(RUN, "call_1")]);
    render(<ApprovalsPage />);
    await screen.findByTestId("approval-sheet");
    await new Promise((resolve) => setTimeout(resolve, 120));
    expect(findApproval).not.toHaveBeenCalled();
  });

  it("stops following, with a notice, when the item is no longer found", async () => {
    const decided = { ...toolItem, status: "decided" as const, decision: "approved" as const, decided_by: "u", decided_at: "2026-11-15T10:01:00+00:00" };
    vi.mocked(listApprovals).mockResolvedValue({ items: [decided], next_cursor: null });
    vi.mocked(findApproval).mockResolvedValue(null);
    nextRouter.reset([routes.approval(RUN, "call_1")]);
    render(<ApprovalsPage />);
    const sheet = await screen.findByTestId("approval-sheet");
    expect(await within(sheet).findByTestId("approval-sheet-notice")).toHaveTextContent("no longer found");
    expect(findApproval).toHaveBeenCalledTimes(1);
    await new Promise((resolve) => setTimeout(resolve, 120));
    expect(findApproval).toHaveBeenCalledTimes(1);
    // The Sheet keeps the copy it had.
    expect(within(sheet).getByTestId("approval-sheet-status")).toHaveTextContent("Approved");
  });

  it("backs off after a failed re-read and stops, with a notice, after three in a row", async () => {
    const decided = { ...toolItem, status: "decided" as const, decision: "approved" as const, decided_by: "u", decided_at: "2026-11-15T10:01:00+00:00" };
    vi.mocked(listApprovals).mockResolvedValue({ items: [decided], next_cursor: null });
    // One failure, a good read (the run still going: the count starts again), then three failures.
    vi.mocked(findApproval)
      .mockRejectedValueOnce(new Error("Server error"))
      .mockResolvedValueOnce({ ...decided, run_status: "running" })
      .mockRejectedValue(new Error("Server error"));
    const started = Date.now();
    nextRouter.reset([routes.approval(RUN, "call_1")]);
    render(<ApprovalsPage />);
    const sheet = await screen.findByTestId("approval-sheet");
    expect(await within(sheet).findByTestId("approval-sheet-notice", {}, { timeout: 3000 })).toHaveTextContent(
      "Stopped following the run: Server error. Reopen the Sheet to try again.",
    );
    expect(findApproval).toHaveBeenCalledTimes(5);
    // 20 + 40 (after a failure) + 20 + 40 + 80 ms at least: the delays grew after each failure.
    expect(Date.now() - started).toBeGreaterThanOrEqual(200);
    await new Promise((resolve) => setTimeout(resolve, 200));
    expect(findApproval).toHaveBeenCalledTimes(5);
  });

  it("does not follow a pending item, and keeps the controls when a re-read has no decision yet", async () => {
    vi.mocked(decideToolApproval).mockResolvedValue({ run_id: RUN, tool_call_id: "call_1", decision: "approved", decided_by: "u", comment: null, notified: true });
    nextRouter.reset([routes.approval(RUN, "call_1")]);
    render(<ApprovalsPage />);
    const sheet = await screen.findByTestId("approval-sheet");
    await within(sheet).findByRole("button", { name: "Approve" });
    await new Promise((resolve) => setTimeout(resolve, 60));
    expect(findApproval).not.toHaveBeenCalled();
    // The projection lags: a re-read without a decision is not put in place.
    vi.mocked(findApproval).mockResolvedValue({ ...toolItem });
    fireEvent.click(within(sheet).getByRole("button", { name: "Approve" }));
    await waitFor(() => expect(vi.mocked(findApproval).mock.calls.length).toBeGreaterThan(1));
    expect(within(sheet).getByTestId("approval-sheet-status")).toHaveTextContent("Approved");
    expect(within(sheet).queryByRole("button", { name: "Approve" })).toBeNull();
  });

  it("links a workflow step to the workflow run view", async () => {
    nextRouter.reset([routes.approval(WF_RUN, "review")]);
    render(<ApprovalsPage />);
    const sheet = await screen.findByTestId("approval-sheet");
    expect(await within(sheet).findByTestId("approval-sheet-open-workflow-run")).toHaveAttribute("href", routes.workflowRun("WF1", WF_RUN));
    expect(within(sheet).queryByTestId("approval-sheet-open-conversation")).toBeNull();
  });

  it("says so when the URL names an approval that is not found", async () => {
    nextRouter.reset([routes.approval(RUN, "nope")]);
    render(<ApprovalsPage />);
    expect(await screen.findByTestId("approval-sheet-missing")).toBeInTheDocument();
    expect(findApproval).toHaveBeenCalledWith(RUN, "nope");
  });

  it("fetches a linked approval that is not on the loaded page, however old", async () => {
    const old: Approval = { ...toolItem, approval_key: "call_old", tool_call_id: "call_old", tool_name: "refund", status: "decided", decision: "denied", decided_at: "2026-01-01T00:00:00+00:00", decided_by: "bob" };
    vi.mocked(findApproval).mockResolvedValue(old);
    nextRouter.reset([routes.approval(RUN, "call_old")]);
    render(<ApprovalsPage />);
    const sheet = await screen.findByTestId("approval-sheet");
    expect(await within(sheet).findByText("Tool call: refund")).toBeInTheDocument();
    expect(within(sheet).getByTestId("approval-sheet-status")).toHaveTextContent("Denied");
    expect(within(sheet).getByTestId("approval-sheet-decided")).toHaveTextContent("by bob");
    expect(findApproval).toHaveBeenCalledWith(RUN, "call_old");
  });

  it("shows a lookup failure apart from not found", async () => {
    vi.mocked(findApproval).mockRejectedValue(new Error("Server error"));
    nextRouter.reset([routes.approval(RUN, "call_x")]);
    render(<ApprovalsPage />);
    expect(await screen.findByTestId("approval-sheet-error")).toHaveTextContent("Server error");
    expect(screen.queryByTestId("approval-sheet-missing")).toBeNull();
    // Retry asks again; this time the item is there.
    vi.mocked(findApproval).mockResolvedValue({ ...toolItem, approval_key: "call_x", tool_call_id: "call_x", tool_name: "lookup_order" });
    fireEvent.click(screen.getByTestId("approval-sheet-retry"));
    expect(await screen.findByText("Tool call: lookup_order")).toBeInTheDocument();
    expect(findApproval).toHaveBeenCalledTimes(2);
    expect(screen.queryByTestId("approval-sheet-error")).toBeNull();
  });

  it("does not offer a decision on a pending item whose run ended", async () => {
    vi.mocked(findApproval).mockResolvedValue({ ...toolItem, approval_key: "call_9", tool_call_id: "call_9", run_status: "failed" });
    nextRouter.reset([routes.approval(RUN, "call_9")]);
    render(<ApprovalsPage />);
    const sheet = await screen.findByTestId("approval-sheet");
    expect(await within(sheet).findByTestId("approval-sheet-status")).toHaveTextContent("Run ended");
    expect(within(sheet).queryByRole("button", { name: "Approve" })).toBeNull();
    expect(within(sheet).getByTestId("approval-sheet-args")).toHaveTextContent("a@example.com");
  });
});
