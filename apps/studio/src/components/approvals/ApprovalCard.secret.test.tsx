import React from "react";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({ putSecret: vi.fn(), decideToolApproval: vi.fn() }));
vi.mock("@/lib/secrets", async (importOriginal) => ({ ...(await importOriginal<object>()), putSecret: mocks.putSecret }));
vi.mock("@/lib/approvals", async (importOriginal) => ({ ...(await importOriginal<object>()), decideToolApproval: mocks.decideToolApproval, refreshPendingApprovals: vi.fn().mockResolvedValue(undefined) }));

import { ApprovalCard } from "./ApprovalCard";
import { InboxApprovalCard } from "./InboxApprovalCard";

const tool = {
  kind: "tool" as const,
  runId: "run-1",
  toolCallId: "call-1",
  toolName: "secrets__request",
  args: { name: "slack-token", purpose: "post alerts" },
  deadline: null,
  summary: "Ask for the secret slack-token",
};
const FAKE = "fake-secret-value-000";

beforeEach(() => {
  mocks.putSecret.mockReset().mockResolvedValue(undefined);
  mocks.decideToolApproval.mockReset().mockResolvedValue({});
});
afterEach(() => cleanup());

describe("ApprovalCard secrets__request", () => {
  it("shows summary, purpose and a masked input; Approve disabled until typed; no args dump", () => {
    render(<ApprovalCard approval={tool} onRefresh={vi.fn()} />);
    expect(screen.getByTestId("approval-summary-tool-call-1")).toHaveTextContent("slack-token");
    const input = screen.getByLabelText(/Value for slack-token/);
    expect(input).toHaveAttribute("type", "password");
    expect(input).toHaveAttribute("autocomplete", "off");
    expect(screen.getByText(/post alerts/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Approve" })).toBeDisabled();
    fireEvent.change(input, { target: { value: FAKE } });
    expect(screen.getByRole("button", { name: "Approve" })).toBeEnabled();
  });

  it("Approve stores the value then approves; the value is not sent to the approve call", async () => {
    const onRefresh = vi.fn();
    render(<ApprovalCard approval={tool} onRefresh={onRefresh} />);
    fireEvent.change(screen.getByLabelText(/Value for slack-token/), { target: { value: FAKE } });
    fireEvent.click(screen.getByRole("button", { name: "Approve" }));
    await waitFor(() => expect(mocks.decideToolApproval).toHaveBeenCalled());
    expect(mocks.putSecret).toHaveBeenCalledWith("slack-token", FAKE);
    expect(mocks.putSecret.mock.invocationCallOrder[0]).toBeLessThan(mocks.decideToolApproval.mock.invocationCallOrder[0]);
    expect(JSON.stringify(mocks.decideToolApproval.mock.calls)).not.toContain(FAKE);
    await waitFor(() => expect(onRefresh).toHaveBeenCalled());
  });

  it("shows a readable error and does not approve when the PUT fails; never echoes the value", async () => {
    mocks.putSecret.mockRejectedValue(new Error("Secret names are lowercase letters, digits and dashes."));
    render(<ApprovalCard approval={tool} onRefresh={vi.fn()} />);
    fireEvent.change(screen.getByLabelText(/Value for slack-token/), { target: { value: FAKE } });
    fireEvent.click(screen.getByRole("button", { name: "Approve" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("lowercase");
    expect(mocks.decideToolApproval).not.toHaveBeenCalled();
    expect(document.body.textContent).not.toContain(FAKE);
  });

  it("Deny is unchanged and sends no secret", async () => {
    render(<ApprovalCard approval={tool} onRefresh={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: "Deny" }));
    fireEvent.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "Deny" }));
    await waitFor(() => expect(mocks.decideToolApproval).toHaveBeenCalledWith("run-1", "call-1", "deny", ""));
    expect(mocks.putSecret).not.toHaveBeenCalled();
  });

  it("other tools keep the plain card", () => {
    render(<ApprovalCard approval={{ ...tool, toolName: "architect__publish_agent" }} onRefresh={vi.fn()} />);
    expect(screen.queryByLabelText(/Value for/)).toBeNull();
    expect(screen.getByRole("button", { name: "Approve" })).toBeEnabled();
  });
});

describe("InboxApprovalCard secrets__request", () => {
  it("renders the masked input", () => {
    const approval = {
      run_id: "run-1", kind: "tool" as const, approval_key: "call-1", tool_call_id: "call-1", step_id: null, root_run_id: "run-1", agent_id: "a", workflow_id: null,
      tool_name: "secrets__request", summary: "Ask", args: { name: "slack-token", purpose: "p" }, prompt: null, status: "pending" as const,
      requested_at: null, deadline: null, decided_at: null, decision: null, decided_by: null, run_status: "waiting",
    };
    render(<InboxApprovalCard approval={approval} onConflict={vi.fn()} onDecided={vi.fn()} />);
    expect(screen.getByLabelText(/Value for slack-token/)).toHaveAttribute("type", "password");
    expect(screen.getByRole("button", { name: "Approve" })).toBeDisabled();
  });
});
