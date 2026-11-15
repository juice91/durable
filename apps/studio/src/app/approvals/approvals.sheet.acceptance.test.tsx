/** AC.15 acceptance: the URL-bound approval Sheet over fetch (the single-item endpoint, 404, Run ended). */
import React from "react";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { nextRouter } from "@/test/nextRouterMock";
import { resetPendingApprovalsForTests } from "@/lib/approvals";
import { routes } from "@/lib/routes";
import ApprovalsPage from "./page";
import { AGENT, RUN, WF_RUN, decidedTool, jsonResponse, stepItem, toolItem } from "./approvalsAcceptanceKit";

vi.mock("next/navigation", async () => (await import("@/test/nextRouterMock")).navigationModule);

const fetchMock = vi.fn();
const urls = () => fetchMock.mock.calls.map((c) => new URL(String(c[0]), "http://x").pathname);
const old = { ...decidedTool, approval_key: "call_old", tool_call_id: "call_old" };

beforeEach(() => {
  nextRouter.reset(["/approvals"]);
  resetPendingApprovalsForTests();
  vi.stubEnv("NEXT_PUBLIC_EAP_DEV_TOKEN", "Bearer test");
  fetchMock.mockReset();
  fetchMock.mockImplementation(async (input: RequestInfo | URL) => {
    const { pathname } = new URL(String(input), "http://x");
    if (pathname.endsWith("/agents")) return jsonResponse([{ id: AGENT, name: "Support bot" }]);
    if (pathname.endsWith(`/approvals/${RUN}/tool/call_old`)) return jsonResponse(old);
    if (pathname.endsWith(`/approvals/${WF_RUN}/step/review`)) return jsonResponse(stepItem);
    if (pathname.includes("/approvals/")) return jsonResponse({ detail: "Approval not found" }, 404);
    if (pathname.endsWith(`/runs/${RUN}`)) return jsonResponse({ id: RUN, conversation_id: "CONV1" });
    return jsonResponse({ items: [toolItem], next_cursor: null });
  });
  vi.stubGlobal("fetch", fetchMock);
});
afterEach(() => {
  resetPendingApprovalsForTests();
  vi.unstubAllEnvs();
  vi.unstubAllGlobals();
});

describe("approval Sheet", () => {
  it("a deep link opens the Sheet and fetches the single item from both kinds", async () => {
    nextRouter.reset([routes.approval(RUN, "call_old")]);
    render(<ApprovalsPage />);
    const sheet = await screen.findByTestId("approval-sheet");
    expect(await within(sheet).findByTestId("approval-sheet-status")).toHaveTextContent("Approved");
    expect(within(sheet).getByTestId("approval-sheet-decided")).toHaveTextContent("by alice");
    expect(urls()).toContain(`/api/v1/approvals/${RUN}/tool/call_old`);
    expect(urls()).toContain(`/api/v1/approvals/${RUN}/step/call_old`);
  });

  it("an unknown item shows the not-found state (404 from both kinds)", async () => {
    nextRouter.reset([routes.approval(RUN, "nope")]);
    render(<ApprovalsPage />);
    expect(await screen.findByTestId("approval-sheet-missing")).toBeInTheDocument();
    expect(screen.queryByTestId("approval-sheet-error")).toBeNull();
    expect(screen.queryByRole("button", { name: "Approve" })).toBeNull();
  });

  it("a malformed id (no colon) also shows not found without a lookup", async () => {
    nextRouter.reset(["/approvals?approval=garbage"]);
    render(<ApprovalsPage />);
    expect(await screen.findByTestId("approval-sheet-missing")).toBeInTheDocument();
    expect(urls().some((p) => p.includes("/approvals/"))).toBe(false);
  });

  it("a pending item of an ended run says Run ended and offers no buttons", async () => {
    fetchMock.mockImplementation(async (input: RequestInfo | URL) => {
      const { pathname } = new URL(String(input), "http://x");
      if (pathname.endsWith("/agents")) return jsonResponse([]);
      if (pathname.endsWith("/tool/call_1")) return jsonResponse({ ...toolItem, run_status: "failed" });
      if (pathname.includes("/approvals/")) return jsonResponse({ detail: "x" }, 404);
      return jsonResponse({ items: [], next_cursor: null });
    });
    nextRouter.reset([routes.approval(RUN, "call_1")]);
    render(<ApprovalsPage />);
    const sheet = await screen.findByTestId("approval-sheet");
    expect(await within(sheet).findByTestId("approval-sheet-status")).toHaveTextContent("Run ended");
    expect(within(sheet).queryByRole("button", { name: /^(Approve|Deny|Reject)$/ })).toBeNull();
    expect(within(sheet).getByTestId("approval-sheet-args")).toHaveTextContent("a@example.com");
  });

  it("a row click pushes the approval into the URL and closing it removes it, keeping the filters", async () => {
    nextRouter.reset([routes.approvals({ kind: "tool" })]);
    render(<ApprovalsPage />);
    fireEvent.click(within(await screen.findByTestId(`approval-row-${RUN}:call_1`)).getByRole("button"));
    await act(() => nextRouter.settle());
    expect(nextRouter.url).toContain("kind=tool");
    expect(new URL(nextRouter.url, "http://x").searchParams.get("approval")).toBe(`${RUN}:call_1`);
    fireEvent.keyDown(await screen.findByTestId("approval-sheet"), { key: "Escape" });
    await act(() => nextRouter.settle());
    await waitFor(() => expect(nextRouter.url).toBe(routes.approvals({ kind: "tool" })));
  });
});
