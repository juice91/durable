import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import React from "react";
import { usePathname } from "next/navigation";
import { routes } from "@/lib/routes";
import { isPendingPollingForTests, PENDING_POLL_MS, resetPendingApprovalsForTests } from "@/lib/approvals";

vi.mock("next/navigation", () => ({
  usePathname: vi.fn(),
  useRouter: vi.fn(() => ({ push: vi.fn(), replace: vi.fn(), back: vi.fn() })),
  useSearchParams: vi.fn(() => new URLSearchParams()),
}));

import { FrameHarness, mockMatchMedia, type MatchMediaControl } from "./frameTesting";

let media: MatchMediaControl;
const fetchMock = vi.fn();

function pendingPage(count: number, nextCursor: string | null = null): Response {
  const items = Array.from({ length: count }, (_, i) => ({ run_id: `r${i}`, approval_key: `k${i}` }));
  return new Response(JSON.stringify({ items, next_cursor: nextCursor }), { status: 200, headers: { "Content-Type": "application/json" } });
}

beforeEach(() => {
  window.localStorage.clear();
  resetPendingApprovalsForTests();
  media = mockMatchMedia(true);
  vi.mocked(usePathname).mockReturnValue(routes.agents());
  vi.stubEnv("NEXT_PUBLIC_EAP_DEV_TOKEN", "Bearer test");
  fetchMock.mockReset();
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(() => {
  resetPendingApprovalsForTests();
  media.restore();
  vi.unstubAllEnvs();
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

const sidebar = () => screen.getByTestId("sidebar");

describe("Approvals nav item and pending badge (AC.15)", () => {
  test("Approvals is the first item, in the untitled top group", () => {
    fetchMock.mockResolvedValue(pendingPage(0));
    render(<FrameHarness />);
    const group = sidebar().querySelector('[data-nav-group="approvals"]')!;
    expect(sidebar().querySelector("[data-nav-group]")).toBe(group);
    expect(within(group as HTMLElement).queryByText("Approvals", { selector: "span[id]" })).toBeNull();
    expect(within(group as HTMLElement).getByRole("link", { name: "Approvals" })).toHaveAttribute("href", routes.approvals());
  });

  test("shows the pending count from GET /approvals?status=pending and hides it at 0", async () => {
    fetchMock.mockResolvedValue(pendingPage(3));
    render(<FrameHarness />);
    expect(await within(sidebar()).findByTestId("nav-badge-approvals")).toHaveTextContent("3");
    expect(within(sidebar()).getByRole("link", { name: "Approvals, 3 pending" })).toBeInTheDocument();
    expect(String(fetchMock.mock.calls[0][0])).toContain("/api/v1/approvals?status=pending");
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  test("hidden at 0; caps at 99+ when there are more pages", async () => {
    fetchMock.mockResolvedValueOnce(pendingPage(0));
    const { unmount } = render(<FrameHarness />);
    await waitFor(() => expect(fetchMock).toHaveBeenCalled());
    expect(within(sidebar()).queryByTestId("nav-badge-approvals")).toBeNull();
    unmount();
    resetPendingApprovalsForTests();
    fetchMock.mockResolvedValue(pendingPage(100, "next"));
    render(<FrameHarness />);
    expect(await within(sidebar()).findByTestId("nav-badge-approvals")).toHaveTextContent("99+");
  });

  test("polls every 30 s", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    fetchMock.mockResolvedValue(pendingPage(1));
    render(<FrameHarness />);
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
    fetchMock.mockResolvedValue(pendingPage(2));
    await act(async () => {
      vi.advanceTimersByTime(PENDING_POLL_MS);
    });
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
    expect(await within(sidebar()).findByTestId("nav-badge-approvals")).toHaveTextContent("2");
  });

  test("stops polling once the last badge unmounts", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    fetchMock.mockResolvedValue(pendingPage(1));
    const { unmount } = render(<FrameHarness />);
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
    expect(isPendingPollingForTests()).toBe(true);
    unmount();
    expect(isPendingPollingForTests()).toBe(false);
    await act(async () => {
      vi.advanceTimersByTime(PENDING_POLL_MS * 3);
    });
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  test("pauses while the tab is hidden and catches up once when it is visible again", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    let visibility: DocumentVisibilityState = "visible";
    const spy = vi.spyOn(document, "visibilityState", "get").mockImplementation(() => visibility);
    try {
      fetchMock.mockResolvedValue(pendingPage(1));
      render(<FrameHarness />);
      await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
      visibility = "hidden";
      act(() => {
        document.dispatchEvent(new Event("visibilitychange"));
      });
      expect(isPendingPollingForTests()).toBe(false);
      await act(async () => {
        vi.advanceTimersByTime(PENDING_POLL_MS * 3);
      });
      expect(fetchMock).toHaveBeenCalledTimes(1);
      fetchMock.mockResolvedValue(pendingPage(5));
      visibility = "visible";
      act(() => {
        document.dispatchEvent(new Event("visibilitychange"));
      });
      await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
      expect(await within(sidebar()).findByTestId("nav-badge-approvals")).toHaveTextContent("5");
      expect(isPendingPollingForTests()).toBe(true);
      await act(async () => {
        vi.advanceTimersByTime(PENDING_POLL_MS);
      });
      await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(3));
    } finally {
      spy.mockRestore();
    }
  });

  test("the 56 px rail shows a dot, and the name carries the count", async () => {
    fetchMock.mockResolvedValue(pendingPage(2));
    render(<FrameHarness />);
    await within(sidebar()).findByTestId("nav-badge-approvals");
    fireEvent.click(screen.getByRole("button", { name: "Collapse sidebar" }));
    expect(await within(sidebar()).findByTestId("nav-badge-dot-approvals")).toBeInTheDocument();
    expect(within(sidebar()).queryByTestId("nav-badge-approvals")).toBeNull();
    expect(within(sidebar()).getByRole("link", { name: "Approvals, 2 pending" })).toBeInTheDocument();
  });

  test("the mobile menu button shows a dot", async () => {
    fetchMock.mockResolvedValue(pendingPage(4));
    render(<FrameHarness />);
    expect(await screen.findByTestId("mobile-nav-badge-dot")).toBeInTheDocument();
    expect(screen.getByTestId("mobile-nav-open")).toHaveAccessibleDescription("4 pending approvals");
  });
});
