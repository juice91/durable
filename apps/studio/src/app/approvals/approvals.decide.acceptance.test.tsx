/** AC.15 acceptance: deciding through ApprovalCard in the Sheet, over fetch (request bodies, 409, links). */
import React from "react";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { nextRouter } from "@/test/nextRouterMock";
import { resetPendingApprovalsForTests, type Approval } from "@/lib/approvals";
import { routes } from "@/lib/routes";
import ApprovalsPage from "./page";
import { AGENT, RUN, WF_RUN, jsonResponse, stepItem, toolItem } from "./approvalsAcceptanceKit";

vi.mock("next/navigation", async () => (await import("@/test/nextRouterMock")).navigationModule);

const fetchMock = vi.fn();
let pendingCount = 1;
let post: (url: string) => Response = () => jsonResponse({ detail: "unexpected" }, 500);
const posts = () => fetchMock.mock.calls.filter((c) => (c[1] as RequestInit | undefined)?.method === "POST");
// The server's copy of the tool call: the pending list holds it only while undecided (the API's
// `decided_at IS NULL`), and GET /approvals/{run}/tool/call_1 always serves it as it is now.
let server: Approval = toolItem;
const decidedBy = (by: string, decision: "approved" | "denied" = "approved"): Approval => ({
  ...toolItem, status: "decided", decision, decided_by: by, decided_at: "2026-11-15T10:05:00+00:00",
});

beforeEach(() => {
  nextRouter.reset([routes.approval(RUN, "call_1")]);
  resetPendingApprovalsForTests();
  pendingCount = 1;
  server = toolItem;
  vi.stubEnv("NEXT_PUBLIC_EAP_DEV_TOKEN", "Bearer test");
  fetchMock.mockReset();
  fetchMock.mockImplementation(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    const { pathname } = new URL(url, "http://x");
    if (init?.method === "POST") return post(url);
    if (pathname.endsWith(`/approvals/${RUN}/tool/call_1`)) return jsonResponse(server);
    if (/\/approvals\/[^/]+\/(tool|step)\//.test(pathname)) return jsonResponse({ detail: "Approval not found" }, 404);
    if (pathname.endsWith("/agents")) return jsonResponse([{ id: AGENT, name: "Support bot" }]);
    if (pathname.endsWith(`/runs/${RUN}`)) return jsonResponse({ id: RUN, conversation_id: "CONV1" });
    if (url.includes("limit=100")) return jsonResponse({ items: Array(pendingCount).fill(toolItem), next_cursor: null });
    const status = new URL(url, "http://x").searchParams.get("status") ?? "pending";
    const tool = (server.status === "pending") === (status === "pending") ? [server] : [];
    return jsonResponse({ items: status === "pending" ? [...tool, stepItem] : tool, next_cursor: null });
  });
  vi.stubGlobal("fetch", fetchMock);
});
afterEach(() => {
  resetPendingApprovalsForTests();
  vi.unstubAllEnvs();
  vi.unstubAllGlobals();
});

describe("deciding in the Sheet", () => {
  it("approve posts to the tool-approval route and the row shows Approved", async () => {
    post = () => {
      server = decidedBy("u");
      return jsonResponse({ run_id: RUN, tool_call_id: "call_1", decision: "approved", decided_by: "u", comment: null, notified: true });
    };
    render(<ApprovalsPage />);
    const sheet = await screen.findByTestId("approval-sheet");
    pendingCount = 0;
    fireEvent.click(await within(sheet).findByRole("button", { name: "Approve" }));
    await waitFor(() => expect(screen.getByTestId(`approval-row-status-${RUN}:call_1`)).toHaveTextContent("Approved"));
    // The re-read item names the decider.
    await waitFor(() => expect(within(screen.getByTestId("approval-sheet")).getByTestId("approval-sheet-decided")).toHaveTextContent("by u"));
    expect(String(posts()[0][0])).toContain(`/runs/${RUN}/tool-approvals/call_1/approve`);
    expect(JSON.parse(String(posts()[0][1].body))).toEqual({});
  });

  it("deny sends the comment after the confirm dialog; the row shows Denied", async () => {
    post = () => {
      server = decidedBy("u", "denied");
      return jsonResponse({ run_id: RUN, tool_call_id: "call_1", decision: "denied", decided_by: "u", comment: "no", notified: true });
    };
    render(<ApprovalsPage />);
    const sheet = await screen.findByTestId("approval-sheet");
    fireEvent.change(await within(sheet).findByLabelText(/Comment/), { target: { value: "x".repeat(500) } });
    expect(within(sheet).getByText("500/500")).toBeInTheDocument();
    fireEvent.click(within(sheet).getByRole("button", { name: "Deny" }));
    const dialog = await screen.findByRole("dialog", { name: /Deny send_email/ });
    fireEvent.click(within(dialog).getByRole("button", { name: "Deny" }));
    await waitFor(() => expect(screen.getByTestId(`approval-row-status-${RUN}:call_1`)).toHaveTextContent("Denied"));
    expect(String(posts()[0][0])).toContain("/tool-approvals/call_1/deny");
    expect(JSON.parse(String(posts()[0][1].body))).toEqual({ comment: "x".repeat(500) });
  });

  it("a comment over 500 characters disables Approve and Deny and sends nothing", async () => {
    render(<ApprovalsPage />);
    const sheet = await screen.findByTestId("approval-sheet");
    fireEvent.change(await within(sheet).findByLabelText(/Comment/), { target: { value: "x".repeat(501) } });
    expect(within(sheet).getByText(/501\/500/)).toBeInTheDocument();
    expect(within(sheet).getByRole("button", { name: "Approve" })).toBeDisabled();
    expect(within(sheet).getByRole("button", { name: "Deny" })).toBeDisabled();
    expect(posts()).toHaveLength(0);
  });

  it("a 409 race keeps the Sheet open on the item and says who decided first", async () => {
    post = () => jsonResponse({ detail: "tool call is already decided", decision: "approved", decided_by: "bob", decided_at: "2026-11-15T10:05:00+00:00" }, 409);
    render(<ApprovalsPage />);
    const sheet = await screen.findByTestId("approval-sheet");
    await within(sheet).findByRole("button", { name: "Approve" });
    // Bob approved it elsewhere: the real pending list no longer holds it.
    server = decidedBy("bob");
    // The Sheet must never unmount on the way (no close and reopen).
    let unmounted = false;
    const observer = new MutationObserver(() => {
      if (!document.body.contains(sheet)) unmounted = true;
    });
    observer.observe(document.body, { childList: true, subtree: true });
    fireEvent.click(within(sheet).getByRole("button", { name: "Approve" }));
    await waitFor(() => expect(posts()).toHaveLength(1));
    const notice = await within(sheet).findByTestId("approval-sheet-notice");
    expect(notice).toHaveAttribute("role", "status");
    expect(notice).toHaveTextContent(/Already approved by bob at .+\. Your decision was not recorded\./);
    expect(within(sheet).getByTestId("approval-sheet-status")).toHaveTextContent("Approved");
    expect(within(sheet).getByTestId("approval-sheet-decided")).toHaveTextContent("by bob");
    expect(within(sheet).queryByRole("button", { name: "Approve" })).toBeNull();
    // The reloaded Pending list dropped the row; the Sheet still shows the item.
    await waitFor(() => expect(screen.queryByTestId(`approval-row-${RUN}:call_1`)).toBeNull());
    expect(screen.getByTestId("approval-sheet")).toBe(sheet);
    expect(within(sheet).getByTestId("approval-sheet-notice")).toBeInTheDocument();
    observer.disconnect();
    expect(unmounted).toBe(false);
    expect(nextRouter.url).toBe(routes.approval(RUN, "call_1"));
  });

  it("links to the conversation, the trace and (for a step) the workflow run", async () => {
    render(<ApprovalsPage />);
    const sheet = await screen.findByTestId("approval-sheet");
    expect(await within(sheet).findByTestId("approval-sheet-open-conversation")).toHaveAttribute("href", routes.agentChat(AGENT, { conversation: "CONV1" }));
    expect(within(sheet).getByTestId("approval-sheet-open-trace")).toHaveAttribute("href", expect.stringContaining(RUN));
    expect(within(sheet).queryByTestId("approval-sheet-open-workflow-run")).toBeNull();
  });

  it("a step's Sheet links to its workflow run and has no conversation link", async () => {
    nextRouter.reset([routes.approval(WF_RUN, "review")]);
    render(<ApprovalsPage />);
    const sheet = await screen.findByTestId("approval-sheet");
    expect(await within(sheet).findByTestId("approval-sheet-open-workflow-run")).toHaveAttribute("href", routes.workflowRun("WF1", WF_RUN));
    expect(within(sheet).queryByTestId("approval-sheet-open-conversation")).toBeNull();
    expect(within(sheet).getByRole("button", { name: "Reject" })).toBeInTheDocument();
  });
});
