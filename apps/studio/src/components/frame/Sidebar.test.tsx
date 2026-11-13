import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import { act, fireEvent, render, screen, within } from "@testing-library/react";
import React from "react";
import { usePathname } from "next/navigation";
import { routes } from "@/lib/routes";
import { navItems } from "@/lib/nav";

vi.mock("next/navigation", () => ({
  usePathname: vi.fn(),
  useRouter: vi.fn(() => ({ push: vi.fn(), replace: vi.fn(), back: vi.fn() })),
  useSearchParams: vi.fn(() => new URLSearchParams()),
}));

import { FrameHarness, mockMatchMedia, type MatchMediaControl } from "./frameTesting";
import { SIDEBAR_STORAGE_KEY } from "./frameStorage";

let media: MatchMediaControl;

beforeEach(() => {
  window.localStorage.clear();
  media = mockMatchMedia(true);
  vi.mocked(usePathname).mockReturnValue(routes.agents());
});

afterEach(() => {
  media.restore();
  vi.unstubAllEnvs();
});

const sidebar = () => screen.getByTestId("sidebar");

describe("Sidebar", () => {
  test("should render all nav labels as links with correct hrefs", () => {
    vi.mocked(usePathname).mockReturnValue(routes.dashboard());
    render(<FrameHarness />);
    for (const item of navItems) {
      const link = within(sidebar()).getByRole("link", { name: item.label });
      expect(link).toHaveAttribute("href", item.href);
    }
  });

  test("should mark active route with aria-current=page for /agents", () => {
    render(<FrameHarness />);
    const agentsLink = sidebar().querySelector(`a[href="${routes.agents()}"]`);
    expect(agentsLink?.getAttribute("aria-current")).toBe("page");
  });

  test("should mark active route with aria-current=page for /", () => {
    vi.mocked(usePathname).mockReturnValue(routes.dashboard());
    render(<FrameHarness />);
    const dashboardLink = sidebar().querySelector(`a[href="${routes.dashboard()}"]`);
    expect(dashboardLink?.getAttribute("aria-current")).toBe("page");
    expect(sidebar().querySelectorAll('[aria-current="page"]')).toHaveLength(1);
  });

  test("an item stays active on its sub-paths", () => {
    vi.mocked(usePathname).mockReturnValue(routes.agentEditor("a1"));
    render(<FrameHarness />);
    expect(within(sidebar()).getByRole("link", { name: "Agents" })).toHaveAttribute("aria-current", "page");
    expect(within(sidebar()).getByRole("link", { name: "Playground" })).not.toHaveAttribute("aria-current");
  });

  test("groups show their titles in order, with Settings in the footer", () => {
    render(<FrameHarness />);
    const groups = Array.from(sidebar().querySelectorAll("[data-nav-group]")).map((el) => el.getAttribute("data-nav-group"));
    expect(groups).toEqual(["primitives", "observability", "settings"]);
    expect(within(sidebar()).getByRole("list", { name: "Primitives" })).toBeInTheDocument();
    expect(within(sidebar()).getByRole("list", { name: "Observability" })).toBeInTheDocument();
    expect(within(screen.getByTestId("sidebar-footer")).getByRole("link", { name: "Settings" })).toBeInTheDocument();
  });

  test("shows the product name and a plain monogram", () => {
    render(<FrameHarness />);
    expect(within(sidebar()).getByText("AI Platform Studio")).toBeInTheDocument();
    expect(within(sidebar()).getByTestId("studio-monogram")).toHaveAttribute("aria-hidden", "true");
  });

  test("the toggle collapses to the icon rail, persists, and announces its shortcut", () => {
    render(<FrameHarness />);
    const toggle = screen.getByRole("button", { name: "Collapse sidebar" });
    expect(toggle).toHaveAttribute("aria-keyshortcuts", "[");
    expect(toggle).toHaveAttribute("aria-expanded", "true");
    fireEvent.click(toggle);
    expect(sidebar()).toHaveAttribute("data-collapsed", "true");
    expect(window.localStorage.getItem(SIDEBAR_STORAGE_KEY)).toBe("collapsed");
    // In the rail the labels are gone from view but stay as each link's accessible name.
    const agents = within(sidebar()).getByRole("link", { name: "Agents" });
    expect(agents).toHaveAttribute("aria-label", "Agents");
    expect(within(sidebar()).queryByText("AI Platform Studio")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Expand sidebar" }));
    expect(sidebar()).toHaveAttribute("data-collapsed", "false");
    expect(window.localStorage.getItem(SIDEBAR_STORAGE_KEY)).toBe("expanded");
  });

  test("restores the collapsed state on load", () => {
    window.localStorage.setItem(SIDEBAR_STORAGE_KEY, "collapsed");
    render(<FrameHarness />);
    expect(sidebar()).toHaveAttribute("data-collapsed", "true");
  });

  test("[ toggles the rail, but not while typing, with a modifier, or below 1024 px", () => {
    render(
      <FrameHarness>
        <input aria-label="Search" />
      </FrameHarness>,
    );
    fireEvent.keyDown(document.body, { key: "[" });
    expect(sidebar()).toHaveAttribute("data-collapsed", "true");
    fireEvent.keyDown(screen.getByRole("textbox", { name: "Search" }), { key: "[" });
    expect(sidebar()).toHaveAttribute("data-collapsed", "true");
    fireEvent.keyDown(document.body, { key: "[", metaKey: true });
    expect(sidebar()).toHaveAttribute("data-collapsed", "true");
    fireEvent.keyDown(document.body, { key: "[" });
    expect(sidebar()).toHaveAttribute("data-collapsed", "false");
    act(() => media.setWide(false));
    fireEvent.keyDown(document.body, { key: "[" });
    expect(sidebar()).toHaveAttribute("data-collapsed", "false");
  });

  test("in the rail, a link's label shows as a tooltip on focus", async () => {
    window.localStorage.setItem(SIDEBAR_STORAGE_KEY, "collapsed");
    render(<FrameHarness />);
    act(() => within(sidebar()).getByRole("link", { name: "Traces" }).focus());
    expect(await screen.findByRole("tooltip")).toHaveTextContent("Traces");
  });

  test("the theme menu stores the chosen theme", async () => {
    render(<FrameHarness />);
    const trigger = within(sidebar()).getByTestId("theme-menu-trigger");
    expect(trigger).toHaveAccessibleName("Theme: System");
    fireEvent.keyDown(trigger, { key: "Enter" });
    fireEvent.click(await screen.findByRole("menuitemradio", { name: "Dark" }));
    expect(window.localStorage.getItem("studio.theme")).toBe("dark");
    expect(document.documentElement.classList.contains("dark")).toBe(true);
  });

  test("shows the version line only when NEXT_PUBLIC_STUDIO_VERSION is set", () => {
    const { unmount } = render(<FrameHarness />);
    expect(screen.queryByTestId("studio-version")).toBeNull();
    unmount();
    vi.stubEnv("NEXT_PUBLIC_STUDIO_VERSION", "1.4.0");
    render(<FrameHarness />);
    expect(within(sidebar()).getByTestId("studio-version")).toHaveTextContent("Version 1.4.0");
  });
});
