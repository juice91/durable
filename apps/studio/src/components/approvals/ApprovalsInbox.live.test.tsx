/** The inbox and the toasts follow the shared pending poll: new cards appear without a reload. */
import React from "react";
import "@testing-library/jest-dom";
import { act, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { nextRouter } from "@/test/nextRouterMock";
import { Toaster, toast } from "@/components/ui/Toaster";
import {
  PENDING_FAST_POLL_MS,
  PENDING_FAST_WINDOW_MS,
  PENDING_POLL_MS,
  noteApprovalDecided,
  resetPendingApprovalsForTests,
  type Approval,
} from "@/lib/approvals";
import { routes } from "@/lib/routes";
import { jsonResponse, toolItem, stepItem } from "@/app/approvals/approvalsAcceptanceKit";
import ApprovalsPage from "@/app/approvals/page";
import { PendingApprovalToasts } from "./PendingApprovalToasts";

vi.mock("next/navigation", async () => (await import("@/test/nextRouterMock")).navigationModule);

const fetchMock = vi.fn();
let pendingItems: Approval[] = [];
const newItem: Approval = { ...stepItem, approval_key: "publish_workflow", step_id: "publish_workflow", summary: "發布工作流程「球棒」，這是一段很長的說明文字，用來確認提示只取前六十個字，所以後面的字不應該出現在提示裡面，再多寫一些字湊滿長度" };

function pendingCalls() {
  return fetchMock.mock.calls.filter(([url]) => String(url).includes("/approvals?status=pending")).length;
}

beforeEach(() => {
  resetPendingApprovalsForTests();
  nextRouter.reset(["/approvals"]);
  vi.stubEnv("NEXT_PUBLIC_EAP_DEV_TOKEN", "Bearer test");
  vi.stubGlobal("fetch", fetchMock);
  fetchMock.mockReset();
  pendingItems = [toolItem];
  fetchMock.mockImplementation(async (url: string) => {
    if (String(url).includes("/approvals?")) return jsonResponse({ items: pendingItems, next_cursor: null });
    if (String(url).includes("/agents")) return jsonResponse([]);
    return jsonResponse({}, 404);
  });
  vi.useFakeTimers({ shouldAdvanceTime: true });
});
afterEach(() => {
  resetPendingApprovalsForTests();
  vi.useRealTimers();
  vi.unstubAllEnvs();
  vi.unstubAllGlobals();
});

const advance = (ms: number) => act(async () => { await vi.advanceTimersByTimeAsync(ms); });

describe("live approvals inbox", () => {
  it("shows a new pending card without load() being called again, and highlights it", async () => {
    render(<ApprovalsPage />);
    await screen.findByTestId(`approval-row-${toolItem.run_id}:call_1`);
    const listCalls = () => fetchMock.mock.calls.filter(([url]) => /approvals\?status=pending&limit=50/.test(String(url))).length;
    const before = listCalls();
    pendingItems = [newItem, toolItem];
    await advance(PENDING_POLL_MS);
    const row = await screen.findByTestId(`approval-row-${newItem.run_id}:publish_workflow`);
    expect(row.parentElement?.firstElementChild).toBe(row); // inserted at the top
    expect(row.className).toContain("ring-ring");
    expect(listCalls()).toBe(before); // no full reload of the list
    await advance(5_100);
    expect(row.className).not.toContain("ring-ring");
  });

  it("updates a card decided elsewhere in place", async () => {
    render(<ApprovalsPage />);
    await screen.findByTestId(`approval-row-${toolItem.run_id}:call_1`);
    const decided = { ...toolItem, status: "decided", decision: "approved", decided_by: "bob", decided_at: "2026-11-15T11:00:00+00:00" };
    pendingItems = [];
    fetchMock.mockImplementation(async (url: string) => {
      if (String(url).includes("/approvals?")) return jsonResponse({ items: [], next_cursor: null });
      if (String(url).includes("/approvals/")) return jsonResponse(decided);
      if (String(url).includes("/agents")) return jsonResponse([]);
      return jsonResponse({}, 404);
    });
    await advance(PENDING_POLL_MS);
    await waitFor(() => expect(screen.getByTestId(`approval-row-status-${toolItem.run_id}:call_1`)).toHaveTextContent(/approved/i));
  });
});

describe("fast poll after a decision", () => {
  it("polls every 3 s for 90 s, then every 30 s again", async () => {
    render(<ApprovalsPage />);
    await screen.findByTestId(`approval-row-${toolItem.run_id}:call_1`);
    await act(async () => { noteApprovalDecided(); });
    const start = pendingCalls();
    await advance(PENDING_FAST_POLL_MS * 3);
    expect(pendingCalls() - start).toBeGreaterThanOrEqual(3);
    await advance(PENDING_FAST_WINDOW_MS);
    const after = pendingCalls();
    await advance(PENDING_FAST_POLL_MS * 5);
    expect(pendingCalls() - after).toBeLessThanOrEqual(1);
    await advance(PENDING_POLL_MS);
    expect(pendingCalls() - after).toBeGreaterThanOrEqual(1);
  });

  it("does not poll while the document is hidden", async () => {
    render(<ApprovalsPage />);
    await screen.findByTestId(`approval-row-${toolItem.run_id}:call_1`);
    await act(async () => { noteApprovalDecided(); });
    const visibility = vi.spyOn(document, "visibilityState", "get").mockReturnValue("hidden");
    document.dispatchEvent(new Event("visibilitychange"));
    const hiddenAt = pendingCalls();
    await advance(PENDING_FAST_POLL_MS * 5);
    expect(pendingCalls()).toBe(hiddenAt);
    visibility.mockRestore();
  });
});

describe("new card toast", () => {
  it("toasts the first 60 characters once with 查看 that opens the card, and announces it", async () => {
    toast.dismiss();
    render(<><PendingApprovalToasts /><Toaster /></>);
    await waitFor(() => expect(pendingCalls()).toBe(1));
    pendingItems = [newItem, toolItem];
    await advance(PENDING_POLL_MS);
    const text = Array.from(newItem.summary as string).slice(0, 60).join("");
    expect(await screen.findByText(`新的審核卡：${text}`, { selector: "[data-title]" })).toBeInTheDocument();
    expect(screen.getByTestId("pending-approval-announcement")).toHaveTextContent(`新的審核卡：${text}`);
    expect(screen.getByTestId("pending-approval-announcement")).toHaveAttribute("role", "status");
    act(() => screen.getByRole("button", { name: "查看" }).click());
    await act(() => nextRouter.settle());
    expect(nextRouter.url).toBe(routes.approval(newItem.run_id, "publish_workflow"));
    await advance(PENDING_POLL_MS);
    expect(toast.getHistory().filter((entry) => String((entry as { title?: unknown }).title).startsWith("新的審核卡"))).toHaveLength(1);
  });
});
