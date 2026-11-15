/** AC.15 acceptance: the Approvals nav item (first, g i, palette) and its pending badge. */
import React from "react";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";

const nav = vi.hoisted(() => ({ push: vi.fn() }));
vi.mock("next/navigation", () => ({
  usePathname: () => "/agents",
  useParams: () => ({}),
  useRouter: () => ({ push: nav.push, replace: vi.fn(), back: vi.fn(), prefetch: vi.fn() }),
  useSearchParams: () => new URLSearchParams(),
}));

import { mockMatchMedia, type MatchMediaControl } from "./frameTesting";
import { KeyboardHarness, pressKey, pressKeys } from "@/components/shortcuts/shortcutsTesting";
import { resetPendingApprovalsForTests } from "@/lib/approvals";
import { routes } from "@/lib/routes";
import { jsonResponse } from "@/app/approvals/approvalsAcceptanceKit";

let media: MatchMediaControl;
const fetchMock = vi.fn();
const page = (n: number, next: string | null = null) =>
  jsonResponse({ items: Array.from({ length: n }, (_, i) => ({ run_id: `r${i}`, approval_key: `k${i}` })), next_cursor: next });
const sidebar = () => screen.getByTestId("sidebar");

beforeEach(() => {
  window.localStorage.clear();
  resetPendingApprovalsForTests();
  media = mockMatchMedia(true);
  nav.push.mockClear();
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

describe("Approvals nav: position, g i, palette", () => {
  test("g i opens /approvals", () => {
    fetchMock.mockResolvedValue(page(0));
    render(<KeyboardHarness />);
    pressKeys("g", "i");
    expect(nav.push).toHaveBeenCalledWith(routes.approvals());
  });

  test("the Approvals link is the first link in the sidebar, in an untitled group", () => {
    fetchMock.mockResolvedValue(page(0));
    render(<KeyboardHarness />);
    const links = within(sidebar()).getAllByRole("link");
    expect(links[0]).toHaveAttribute("data-nav-id", "approvals");
    expect(links[0]).toHaveAttribute("href", "/approvals");
    expect(links[0].closest("[data-nav-group]")).toHaveAttribute("data-nav-group", "approvals");
  });

  test("the palette lists Approvals and Enter opens it", async () => {
    fetchMock.mockResolvedValue(page(0));
    render(<KeyboardHarness />);
    pressKey("k", { metaKey: true });
    const dialog = await screen.findByRole("dialog", { name: "Command palette" });
    const input = screen.getByTestId("command-palette-input");
    fireEvent.change(input, { target: { value: "approvals" } });
    await waitFor(() => expect(dialog.querySelector('[data-palette-page="approvals"]')).not.toBeNull());
    fireEvent.keyDown(input, { key: "Enter" });
    expect(nav.push).toHaveBeenCalledWith(routes.approvals());
  });
});
