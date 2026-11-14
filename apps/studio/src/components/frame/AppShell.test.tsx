import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import React, { useEffect } from "react";
import { usePathname } from "next/navigation";
import { routes } from "@/lib/routes";
import { expectNoAxeViolations, AXE_TIMEOUT_MS, PAGE_RULES } from "@/test/a11y";

vi.mock("next/navigation", () => ({
  usePathname: vi.fn(),
  useRouter: vi.fn(() => ({ push: vi.fn(), replace: vi.fn(), back: vi.fn() })),
  useSearchParams: vi.fn(() => new URLSearchParams()),
}));

import { IconButton } from "@/components/ui/IconButton";
import { FrameHarness, mockMatchMedia, type MatchMediaControl } from "./frameTesting";
import { PageHeader } from "./PageHeader";
import { useFrame } from "./FrameContext";

let media: MatchMediaControl;

beforeEach(() => {
  // The root layout sets lang="en" on <html>; jsdom's document has none.
  document.documentElement.lang = "en";
  window.localStorage.clear();
  vi.mocked(usePathname).mockReturnValue(routes.agents());
});

afterEach(() => {
  media?.restore();
});

function Page() {
  return (
    <>
      <PageHeader crumbs={[{ label: "Agents" }]} actions={<IconButton label="Refresh" icon={<span aria-hidden="true">R</span>} />} />
      <p>Body</p>
    </>
  );
}

describe("AppShell", () => {
  test("has one main landmark, a skip link first in the tab order, and the page inside the card", () => {
    media = mockMatchMedia(true);
    render(<FrameHarness><Page /></FrameHarness>);
    const mains = screen.getAllByRole("main");
    expect(mains).toHaveLength(1);
    expect(mains[0]).toHaveAttribute("id", "main");
    expect(within(mains[0]).getByRole("heading", { level: 1, name: "Agents" })).toBeInTheDocument();
    const skip = screen.getByRole("link", { name: "Skip to content" });
    expect(skip).toHaveAttribute("href", "#main");
    expect(document.body.querySelector("a, button, input, [tabindex]:not([tabindex='-1'])")).toBe(skip);
  });

  test("the skip link moves focus to main", () => {
    media = mockMatchMedia(true);
    render(<FrameHarness><Page /></FrameHarness>);
    fireEvent.click(screen.getByRole("link", { name: "Skip to content" }));
    expect(screen.getByRole("main")).toHaveFocus();
  });

  test("mounts the tooltip provider: a page's tooltip trigger renders and opens", async () => {
    media = mockMatchMedia(true);
    render(<FrameHarness><Page /></FrameHarness>);
    act(() => screen.getByRole("button", { name: "Refresh" }).focus());
    expect(await screen.findByRole("tooltip")).toHaveTextContent("Refresh");
  });

  test("no TopBar or fake avatar", () => {
    media = mockMatchMedia(true);
    render(<FrameHarness><Page /></FrameHarness>);
    expect(screen.queryByText("US")).toBeNull();
  });

  describe("below 1024 px", () => {
    beforeEach(() => {
      media = mockMatchMedia(false);
    });

    test("the top bar's menu button opens the navigation drawer, which traps focus", async () => {
      render(<FrameHarness><Page /></FrameHarness>);
      const bar = screen.getByTestId("mobile-top-bar");
      expect(within(bar).getByText("AI Platform Studio")).toBeInTheDocument();
      const open = within(bar).getByRole("button", { name: "Open navigation" });
      expect(open).toHaveAttribute("aria-expanded", "false");
      act(() => open.focus());
      fireEvent.click(open);
      const drawer = await screen.findByRole("dialog", { name: "Navigation" });
      expect(within(drawer).getByText("AI Platform Studio")).toBeInTheDocument();
      expect(open).toHaveAttribute("aria-expanded", "true");
      await waitFor(() => expect(drawer).toContainElement(document.activeElement as HTMLElement));
      expect(within(drawer).getByRole("link", { name: "Agents" })).toHaveAttribute("aria-current", "page");
      expect(within(drawer).getByRole("link", { name: "Settings" })).toBeInTheDocument();
      // Everything outside the drawer is hidden from assistive tech while it is open.
      expect(screen.queryByRole("main")).toBeNull();
    });

    test("Escape closes the drawer and focus returns to the menu button", async () => {
      render(<FrameHarness><Page /></FrameHarness>);
      const open = screen.getByRole("button", { name: "Open navigation" });
      act(() => open.focus());
      fireEvent.click(open);
      const drawer = await screen.findByRole("dialog");
      await waitFor(() => expect(drawer).toContainElement(document.activeElement as HTMLElement));
      fireEvent.keyDown(document.activeElement as HTMLElement, { key: "Escape" });
      await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
      await waitFor(() => expect(open).toHaveFocus());
    });

    test("following a link closes the drawer", async () => {
      render(<FrameHarness><Page /></FrameHarness>);
      fireEvent.click(screen.getByRole("button", { name: "Open navigation" }));
      const drawer = await screen.findByRole("dialog");
      // jsdom cannot navigate; the click handler is what is under test.
      const stopNavigation = (event: Event) => event.preventDefault();
      document.addEventListener("click", stopNavigation);
      fireEvent.click(within(drawer).getByRole("link", { name: "Traces" }));
      document.removeEventListener("click", stopNavigation);
      expect(screen.queryByRole("dialog")).toBeNull();
      // Focus lands on the page, not back on the menu button.
      await waitFor(() => expect(screen.getByRole("main")).toHaveFocus());
    });

    test("a route change closes the drawer", async () => {
      const { rerender } = render(<FrameHarness><Page /></FrameHarness>);
      fireEvent.click(screen.getByRole("button", { name: "Open navigation" }));
      await screen.findByRole("dialog");
      vi.mocked(usePathname).mockReturnValue(routes.traces());
      rerender(<FrameHarness><Page /></FrameHarness>);
      expect(screen.queryByRole("dialog")).toBeNull();
    });

    test("widening the window closes the drawer", async () => {
      render(<FrameHarness><Page /></FrameHarness>);
      fireEvent.click(screen.getByRole("button", { name: "Open navigation" }));
      await screen.findByRole("dialog");
      act(() => media.setWide(true));
      expect(screen.queryByRole("dialog")).toBeNull();
    });

    test("the search button appears once a palette registers, and opens it", () => {
      const openPalette = vi.fn();
      function Palette() {
        const frame = useFrame();
        const register = frame?.registerPalette;
        useEffect(() => register?.(openPalette), [register]);
        return null;
      }
      const { rerender } = render(<FrameHarness><Page /></FrameHarness>);
      expect(screen.queryByTestId("mobile-search")).toBeNull();
      rerender(<FrameHarness><Page /><Palette /></FrameHarness>);
      fireEvent.click(screen.getByTestId("mobile-search"));
      expect(openPalette).toHaveBeenCalledTimes(1);
    });
  });

  describe("axe", () => {
    test("the shell has no violations, with the page-level rules on", async () => {
      media = mockMatchMedia(true);
      render(<FrameHarness><Page /></FrameHarness>);
      await expectNoAxeViolations(document, { rules: PAGE_RULES });
    }, AXE_TIMEOUT_MS);

    test("the shell with the rail collapsed has no violations", async () => {
      media = mockMatchMedia(true);
      window.localStorage.setItem("studio.sidebar", "collapsed");
      render(<FrameHarness><Page /></FrameHarness>);
      await expectNoAxeViolations(document);
    }, AXE_TIMEOUT_MS);

    test("the open navigation drawer has no violations", async () => {
      media = mockMatchMedia(false);
      render(<FrameHarness><Page /></FrameHarness>);
      fireEvent.click(screen.getByRole("button", { name: "Open navigation" }));
      await screen.findByRole("dialog");
      await expectNoAxeViolations(document.body);
    }, AXE_TIMEOUT_MS);
  });
});
