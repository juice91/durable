import React from "react";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import "@testing-library/jest-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const mocks = vi.hoisted(() => ({ getApproval: vi.fn(), decideToolApproval: vi.fn() }));
vi.mock("@/lib/approvals", async (importOriginal) => ({
  ...(await importOriginal<object>()),
  getApproval: mocks.getApproval,
  decideToolApproval: mocks.decideToolApproval,
  refreshPendingApprovals: vi.fn().mockResolvedValue(undefined),
}));
vi.mock("@/components/architect/ProvenanceFooter", () => ({ ProvenanceFooter: () => null }));

import { ApprovalCard } from "./ApprovalCard";
import { isPublishTool } from "./PublishGrantsLine";

const card = (toolName = "architect__publish_agent") => (
  <ApprovalCard approval={{ kind: "tool", runId: "run-1", toolCallId: "call-1", toolName, args: { id: "helper" }, deadline: null }} onRefresh={vi.fn()} />
);
const grants = (canApprove: boolean) => ({
  newly_uses: { mcp_servers: ["tickets"], secrets: ["crm_token"] },
  can_approve: canApprove,
  message: "This version newly uses MCP server `tickets`, secret `crm_token`. Approving grants them.",
});

beforeEach(() => {
  mocks.getApproval.mockReset();
  mocks.decideToolApproval.mockReset().mockResolvedValue({});
});
afterEach(() => cleanup());

describe("the publish card's grant line", () => {
  it("lists what the version newly uses and leaves Approve to an admin", async () => {
    mocks.getApproval.mockResolvedValue({ grants: grants(true) });
    render(card());
    const line = await screen.findByTestId("approval-grants-call-1");
    expect(line).toHaveTextContent("newly uses MCP server `tickets`, secret `crm_token`");
    expect(line).toHaveTextContent("Approving grants them.");
    expect(line).not.toHaveTextContent("An admin must approve");
    expect(screen.getByRole("button", { name: "Approve" })).toBeEnabled();
  });

  it("tells a person who is not an admin that an admin must approve, and disables Approve (Deny stays)", async () => {
    mocks.getApproval.mockResolvedValue({ grants: grants(false) });
    render(card("control__agent_publish"));
    expect(await screen.findByText("An admin must approve this publish.")).toBeInTheDocument();
    await waitFor(() => expect(screen.getByRole("button", { name: "Approve" })).toBeDisabled());
    expect(screen.getByRole("button", { name: "Deny" })).toBeEnabled();
  });

  it("shows no line when nothing is new", async () => {
    mocks.getApproval.mockResolvedValue({ grants: null });
    render(card());
    await waitFor(() => expect(mocks.getApproval).toHaveBeenCalled());
    expect(screen.queryByTestId("approval-grants-call-1")).toBeNull();
    expect(screen.getByRole("button", { name: "Approve" })).toBeEnabled();
  });

  it("works without the line when it cannot be read", async () => {
    mocks.getApproval.mockRejectedValue(new Error("down"));
    render(card("architect__restore_agent_version"));
    await waitFor(() => expect(mocks.getApproval).toHaveBeenCalled());
    expect(screen.queryByTestId("approval-grants-call-1")).toBeNull();
    expect(screen.getByRole("button", { name: "Approve" })).toBeEnabled();
  });

  it("does not ask for another tool", () => {
    render(card("http__request"));
    expect(mocks.getApproval).not.toHaveBeenCalled();
  });

  it("knows the publishing tools by either spelling", () => {
    for (const name of ["architect.publish_agent", "architect__publish_workflow", "control__agent_publish", "control.workflow_publish", "architect__restore_workflow_version"]) {
      expect(isPublishTool(name), name).toBe(true);
    }
    expect(isPublishTool("architect__archive_agent")).toBe(false);
  });
});
