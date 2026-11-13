import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import fs from "fs";
import path from "path";
import React from "react";
import { renderToStaticMarkup, renderToString } from "react-dom/server";
import { hydrateRoot, type Root } from "react-dom/client";
import { act } from "@testing-library/react";
import { themeScript } from "@/lib/theme";
import { SIDEBAR_ATTRIBUTE, SIDEBAR_STORAGE_KEY } from "@/lib/sidebarPref";

vi.mock("next/navigation", () => ({
  usePathname: vi.fn(() => "/agents"),
  useRouter: vi.fn(() => ({ push: vi.fn(), replace: vi.fn(), back: vi.fn() })),
  useSearchParams: vi.fn(() => new URLSearchParams()),
}));

import { FrameHarness, mockMatchMedia, type MatchMediaControl } from "./frameTesting";
import { useFrame } from "./FrameContext";

// The sidebar's no-flash contract: the head script copies the stored state to <html data-sidebar>
// before paint, and CSS shapes the server's (expanded) markup into the rail from it. On a client
// render the frame's first render already reads the attribute. When hydrating, React's first pass
// must match the server markup (expanded), then it re-renders as the rail before the browser paints
// the result; the CSS keeps the rail's shape throughout, so nothing visibly flips.

const root = document.documentElement;
const runHeadScript = () => new Function(themeScript())();
let media: MatchMediaControl;

beforeEach(() => {
  window.localStorage.clear();
  root.removeAttribute(SIDEBAR_ATTRIBUTE);
  root.className = "";
  root.removeAttribute("style");
  media = mockMatchMedia(true);
});

afterEach(() => {
  media.restore();
  vi.restoreAllMocks();
  root.removeAttribute(SIDEBAR_ATTRIBUTE);
});

type Stored = "collapsed" | "expanded" | "garbage" | "absent" | "throws";
const STORED: Stored[] = ["collapsed", "expanded", "garbage", "absent", "throws"];

function seed(stored: Stored) {
  if (stored === "throws") {
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new Error("SecurityError");
    });
  } else if (stored !== "absent") {
    window.localStorage.setItem(SIDEBAR_STORAGE_KEY, stored);
  }
}

/** Every render's view of the collapsed state, in order. */
function Recorder({ seen }: { seen: boolean[] }) {
  const frame = useFrame();
  seen.push(frame?.sidebarCollapsed ?? false);
  return null;
}

function watchSidebarAttribute() {
  const observer = new MutationObserver(() => {});
  observer.observe(root, { attributes: true, attributeOldValue: true, attributeFilter: [SIDEBAR_ATTRIBUTE] });
  return () => {
    const records = observer.takeRecords();
    observer.disconnect();
    return records.filter((record) => record.oldValue !== root.getAttribute(SIDEBAR_ATTRIBUTE));
  };
}

describe("sidebar no-flash contract", () => {
  it.each(STORED)("stored %s: head script, then a frame whose first render already matches", (stored) => {
    seed(stored);
    runHeadScript();
    const collapsed = stored === "collapsed";
    expect(root.getAttribute(SIDEBAR_ATTRIBUTE)).toBe(collapsed ? "collapsed" : null);

    const stop = watchSidebarAttribute();
    const seen: boolean[] = [];
    render(
      <FrameHarness>
        <Recorder seen={seen} />
      </FrameHarness>,
    );
    expect(stop()).toEqual([]);
    expect(seen.length).toBeGreaterThan(0);
    expect(new Set(seen)).toEqual(new Set([collapsed]));
    expect(screen.getByTestId("sidebar")).toHaveAttribute("data-collapsed", String(collapsed));
  });

  it("a stale attribute is cleared when storage says expanded", () => {
    root.setAttribute(SIDEBAR_ATTRIBUTE, "collapsed");
    window.localStorage.setItem(SIDEBAR_STORAGE_KEY, "expanded");
    runHeadScript();
    expect(root.hasAttribute(SIDEBAR_ATTRIBUTE)).toBe(false);
  });

  it("a failing theme step does not stop the sidebar step", () => {
    window.localStorage.setItem(SIDEBAR_STORAGE_KEY, "collapsed");
    window.localStorage.setItem("studio.theme", "system");
    window.matchMedia = vi.fn(() => {
      throw new Error("no media");
    }) as typeof window.matchMedia;
    expect(() => runHeadScript()).not.toThrow();
    expect(root.getAttribute(SIDEBAR_ATTRIBUTE)).toBe("collapsed");
  });

  it("toggling writes storage and the attribute together", () => {
    render(<FrameHarness />);
    fireEvent.click(screen.getByRole("button", { name: "Collapse sidebar" }));
    expect(root.getAttribute(SIDEBAR_ATTRIBUTE)).toBe("collapsed");
    expect(window.localStorage.getItem(SIDEBAR_STORAGE_KEY)).toBe("collapsed");
    fireEvent.click(screen.getByRole("button", { name: "Expand sidebar" }));
    expect(root.hasAttribute(SIDEBAR_ATTRIBUTE)).toBe(false);
    expect(window.localStorage.getItem(SIDEBAR_STORAGE_KEY)).toBe("expanded");
  });

  it("the frame follows the attribute when something else changes it", async () => {
    render(<FrameHarness />);
    root.setAttribute(SIDEBAR_ATTRIBUTE, "collapsed");
    expect(await screen.findByRole("button", { name: "Expand sidebar" })).toBeInTheDocument();
  });

  it("hydrating with data-sidebar preset: no hydration errors, and the frame ends as the rail", async () => {
    const container = document.createElement("div");
    container.innerHTML = renderToString(<FrameHarness />);
    expect(container.querySelector('[data-testid="sidebar"]')).toHaveAttribute("data-collapsed", "false");
    // What the head script does before the bundle runs.
    window.localStorage.setItem(SIDEBAR_STORAGE_KEY, "collapsed");
    runHeadScript();
    expect(root.getAttribute(SIDEBAR_ATTRIBUTE)).toBe("collapsed");
    document.body.appendChild(container);
    const errors = vi.spyOn(console, "error").mockImplementation(() => {});
    const stop = watchSidebarAttribute();
    let reactRoot: Root | undefined;
    await act(async () => {
      reactRoot = hydrateRoot(container, <FrameHarness />);
    });
    expect(errors).not.toHaveBeenCalled();
    expect(stop()).toEqual([]);
    expect(container.querySelector('[data-testid="sidebar"]')).toHaveAttribute("data-collapsed", "true");
    expect(container.querySelector('[data-testid="sidebar-toggle"]')).toHaveAccessibleName("Expand sidebar");
    act(() => reactRoot?.unmount());
    container.remove();
  });

  it("the server renders the expanded sidebar with the hooks the pre-hydration CSS needs", () => {
    const html = renderToStaticMarkup(<FrameHarness />);
    expect(html).toContain('data-sidebar-root=""');
    expect(html).toContain('data-sidebar-head=""');
    expect(html).toMatch(/data-rail-hidden=""[^>]*>Agents</);
    expect(html).toMatch(/data-rail-item=""/);
    expect(html).toContain('data-collapsed="false"');

    const css = fs.readFileSync(path.join(__dirname, "../../app/globals.css"), "utf8");
    expect(css).toMatch(/html\[data-sidebar="collapsed"\] \[data-sidebar-root\] \{\s*width: 3\.5rem;/);
    expect(css).toMatch(/html\[data-sidebar="collapsed"\] \[data-sidebar-root\] \[data-rail-hidden\]/);
  });
});
