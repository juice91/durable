/** AC.15 acceptance: the pending badge (poll, hide at 0, rail dot, mobile dot, refresh after a decision). */
import React from "react";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";

vi.mock("next/navigation", () => ({
  usePathname: () => "/agents",
  useParams: () => ({}),
  useRouter: () => ({ push: vi.fn(), replace: vi.fn(), back: vi.fn(), prefetch: vi.fn() }),
  useSearchParams: () => new URLSearchParams(),
}));

import { FrameHarness, mockMatchMedia, type MatchMediaControl } from "./frameTesting";
import { PENDING_POLL_MS, refreshPendingApprovals, resetPendingApprovalsForTests } from "@/lib/approvals";
import { jsonResponse } from "@/app/approvals/approvalsAcceptanceKit";

let media: MatchMediaControl;
const fetchMock = vi.fn();
const page = (n: number) => jsonResponse({ items: Array.from({ length: n }, (_, i) => ({ run_id: `r${i}`, approval_key: `k${i}` })), next_cursor: null });
const sidebar = () => screen.getByTestId("sidebar");
const badge = () => within(sidebar()).queryByTestId("nav-badge-approvals");

beforeEach(() => {
  window.localStorage.clear();
  resetPendingApprovalsForTests();
  media = mockMatchMedia(true);
  fetchMock.mockReset();
  vi.stubEnv("NEXT_PUBLIC_EAP_DEV_TOKEN", "Bearer test");
  vi.stubGlobal("fetch", fetchMock);
});
afterEach(() => {
  resetPendingApprovalsForTests();
  media.restore();
  vi.unstubAllEnvs();
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

describe("pending badge", () => {
  test("polls every 30 s with fake timers, and a count that drops to 0 hides the badge", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    fetchMock.mockImplementation(async () => page(3));
    render(<FrameHarness />);
    await waitFor(() => expect(badge()).toHaveTextContent("3"));
    expect(fetchMock).toHaveBeenCalledTimes(1);
    await act(async () => { vi.advanceTimersByTime(PENDING_POLL_MS - 1000); });
    expect(fetchMock).toHaveBeenCalledTimes(1);
    fetchMock.mockImplementation(async () => page(0));
    await act(async () => { vi.advanceTimersByTime(1000); });
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(badge()).toBeNull());
    expect(within(sidebar()).getByRole("link", { name: "Approvals" })).toBeInTheDocument();
  });

  test("a failed poll keeps the last count", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    fetchMock.mockImplementationOnce(async () => page(2));
    render(<FrameHarness />);
    await waitFor(() => expect(badge()).toHaveTextContent("2"));
    fetchMock.mockImplementation(async () => jsonResponse({ detail: "boom" }, 500));
    await act(async () => { vi.advanceTimersByTime(PENDING_POLL_MS); });
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
    expect(badge()).toHaveTextContent("2");
  });

  test("refreshPendingApprovals (after a decision) updates the badge without waiting for the poll", async () => {
    fetchMock.mockImplementationOnce(async () => page(2));
    render(<FrameHarness />);
    await waitFor(() => expect(badge()).toHaveTextContent("2"));
    fetchMock.mockImplementation(async () => page(1));
    await act(async () => { await refreshPendingApprovals(); });
    expect(badge()).toHaveTextContent("1");
  });

  test("two consumers (sidebar and mobile menu) share one poll", async () => {
    fetchMock.mockImplementation(async () => page(5));
    render(<FrameHarness />);
    expect(await screen.findByTestId("mobile-nav-badge-dot")).toBeInTheDocument();
    await waitFor(() => expect(badge()).toHaveTextContent("5"));
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  test("no dot on mobile and none in the rail while nothing is pending", async () => {
    fetchMock.mockImplementation(async () => page(0));
    render(<FrameHarness />);
    await waitFor(() => expect(fetchMock).toHaveBeenCalled());
    fireEvent.click(screen.getByRole("button", { name: "Collapse sidebar" }));
    expect(screen.queryByTestId("mobile-nav-badge-dot")).toBeNull();
    expect(screen.queryByTestId("nav-badge-dot-approvals")).toBeNull();
  });
});
