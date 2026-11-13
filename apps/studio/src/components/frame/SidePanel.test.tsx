import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import React, { useState } from "react";
import { renderToString } from "react-dom/server";
import { hydrateRoot, type Root } from "react-dom/client";
import { usePathname } from "next/navigation";

vi.mock("next/navigation", () => ({
  usePathname: vi.fn(() => "/agents/a1/chat"),
  useRouter: vi.fn(() => ({ push: vi.fn(), replace: vi.fn(), back: vi.fn() })),
  useSearchParams: vi.fn(() => new URLSearchParams()),
}));

import { FrameHarness, mockMatchMedia, type MatchMediaControl } from "./frameTesting";
import { SidePanel, useSidePanel } from "./SidePanel";
import {
  clampSidePanelWidth,
  readSidebarState,
  readSidePanelWidth,
  SIDE_PANEL_DEFAULT_WIDTH,
  SIDE_PANEL_MIN_WIDTH,
  SIDE_PANEL_STORAGE_KEY,
  SIDEBAR_STORAGE_KEY,
  sidePanelMaxWidth,
  writeSidePanelWidth,
} from "./frameStorage";

let media: MatchMediaControl;
const originalWidth = window.innerWidth;

function setViewport(width: number) {
  Object.defineProperty(window, "innerWidth", { configurable: true, writable: true, value: width });
  window.dispatchEvent(new Event("resize"));
}

beforeEach(() => {
  window.localStorage.clear();
  vi.mocked(usePathname).mockReturnValue("/agents/a1/chat");
  setViewport(1600);
});

afterEach(() => {
  media?.restore();
  setViewport(originalWidth);
  vi.unstubAllGlobals();
});

function ConfigToggle() {
  const panel = useSidePanel();
  return (
    <button type="button" aria-pressed={panel.open} disabled={!panel.available} onClick={panel.toggle}>
      Config
    </button>
  );
}

function AgentPage({ withPanel = true, defaultOpen = true, title = "Config" }: { withPanel?: boolean; defaultOpen?: boolean; title?: string }) {
  return (
    <>
      <ConfigToggle />
      {withPanel ? (
        <SidePanel title={title} defaultOpen={defaultOpen}>
          <p>Model: gpt-test</p>
        </SidePanel>
      ) : null}
    </>
  );
}

describe("SidePanel (1024 px and up)", () => {
  beforeEach(() => {
    media = mockMatchMedia(true);
  });

  test("a registered panel opens beside the main card, outside it, with its title", () => {
    render(<FrameHarness><AgentPage /></FrameHarness>);
    const panel = screen.getByRole("complementary", { name: "Config" });
    expect(within(panel).getByText("Model: gpt-test")).toBeInTheDocument();
    expect(screen.getByRole("main")).not.toContainElement(panel);
    expect(panel).toHaveStyle({ width: `${SIDE_PANEL_DEFAULT_WIDTH}px` });
  });

  test("collapsed by default unless the page asks; the toggle opens and closes it", () => {
    render(<FrameHarness><AgentPage defaultOpen={false} /></FrameHarness>);
    expect(screen.queryByTestId("side-panel")).toBeNull();
    const toggle = screen.getByRole("button", { name: "Config" });
    expect(toggle).toBeEnabled();
    fireEvent.click(toggle);
    expect(screen.getByRole("complementary", { name: "Config" })).toBeInTheDocument();
    expect(toggle).toHaveAttribute("aria-pressed", "true");
    fireEvent.click(screen.getByRole("button", { name: "Close Config" }));
    expect(screen.queryByTestId("side-panel")).toBeNull();
    expect(toggle).toHaveAttribute("aria-pressed", "false");
  });

  test("unmounting the page unregisters the panel", () => {
    function Host() {
      const [shown, setShown] = useState(true);
      return (
        <>
          <button type="button" onClick={() => setShown(false)}>Leave</button>
          <ConfigToggle />
          {shown ? <SidePanel title="Config" defaultOpen><p>Model: gpt-test</p></SidePanel> : null}
        </>
      );
    }
    render(<FrameHarness><Host /></FrameHarness>);
    expect(screen.getByTestId("side-panel")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Leave" }));
    expect(screen.queryByTestId("side-panel")).toBeNull();
    expect(screen.getByRole("button", { name: "Config" })).toBeDisabled();
  });

  test("a title change keeps the panel open", () => {
    const { rerender } = render(<FrameHarness><AgentPage title="Config" /></FrameHarness>);
    rerender(<FrameHarness><AgentPage title="Agent config" /></FrameHarness>);
    expect(screen.getByRole("complementary", { name: "Agent config" })).toBeInTheDocument();
  });

  test("the handle resizes by keyboard within 360 px and half the window, and saves the width", () => {
    render(<FrameHarness><AgentPage /></FrameHarness>);
    const handle = screen.getByRole("separator", { name: "Resize side panel" });
    expect(handle).toHaveAttribute("aria-valuenow", "400");
    expect(handle).toHaveAttribute("aria-valuemin", String(SIDE_PANEL_MIN_WIDTH));
    expect(handle).toHaveAttribute("aria-valuemax", "800");
    fireEvent.keyDown(handle, { key: "ArrowLeft" });
    expect(handle).toHaveAttribute("aria-valuenow", "416");
    expect(JSON.parse(window.localStorage.getItem(SIDE_PANEL_STORAGE_KEY)!)).toEqual({ width: 416 });
    fireEvent.keyDown(handle, { key: "Home" });
    expect(handle).toHaveAttribute("aria-valuenow", "360");
    fireEvent.keyDown(handle, { key: "ArrowRight" });
    expect(handle).toHaveAttribute("aria-valuenow", "360");
    fireEvent.keyDown(handle, { key: "End" });
    expect(handle).toHaveAttribute("aria-valuenow", "800");
    expect(screen.getByTestId("side-panel")).toHaveStyle({ width: "800px" });
  });

  test("dragging the handle left widens the panel and saves on release", () => {
    // jsdom has no PointerEvent; a MouseEvent carries button and clientX.
    class PointerEventShim extends MouseEvent {
      pointerId: number;
      constructor(type: string, init: PointerEventInit = {}) {
        super(type, init);
        this.pointerId = init.pointerId ?? 0;
      }
    }
    vi.stubGlobal("PointerEvent", PointerEventShim);
    render(<FrameHarness><AgentPage /></FrameHarness>);
    const handle = screen.getByRole("separator", { name: "Resize side panel" });
    fireEvent.pointerDown(handle, { button: 0, clientX: 1000, pointerId: 1 });
    fireEvent.pointerMove(handle, { clientX: 900, pointerId: 1 });
    expect(handle).toHaveAttribute("aria-valuenow", "500");
    expect(window.localStorage.getItem(SIDE_PANEL_STORAGE_KEY)).toBeNull();
    fireEvent.pointerUp(handle, { clientX: 900, pointerId: 1 });
    expect(JSON.parse(window.localStorage.getItem(SIDE_PANEL_STORAGE_KEY)!)).toEqual({ width: 500 });
  vi.unstubAllGlobals();
  });

  test("listens to window resizes only while the panel is open", () => {
    const add = vi.spyOn(window, "addEventListener");
    const remove = vi.spyOn(window, "removeEventListener");
    const resizeCalls = (spy: typeof add) => spy.mock.calls.filter(([type]) => type === "resize").length;
    render(<FrameHarness><AgentPage defaultOpen={false} /></FrameHarness>);
    expect(resizeCalls(add)).toBe(0);
    fireEvent.click(screen.getByRole("button", { name: "Config" }));
    expect(resizeCalls(add)).toBe(1);
    fireEvent.click(screen.getByRole("button", { name: "Close Config" }));
    expect(resizeCalls(remove)).toBe(1);
    add.mockRestore();
    remove.mockRestore();
  });

  test("restores the saved width, clamped to half the window", () => {
    writeSidePanelWidth(700);
    render(<FrameHarness><AgentPage /></FrameHarness>);
    expect(screen.getByTestId("side-panel")).toHaveStyle({ width: "700px" });
    act(() => setViewport(1100));
    expect(screen.getByTestId("side-panel")).toHaveStyle({ width: "550px" });
  });
});

describe("SidePanel below 1024 px", () => {
  test("opens as a Sheet instead of a column, and closes with its Close button", async () => {
    media = mockMatchMedia(false);
    render(<FrameHarness><AgentPage defaultOpen={false} /></FrameHarness>);
    expect(screen.queryByRole("complementary", { name: "Config" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Config" }));
    const sheet = await screen.findByRole("dialog", { name: "Config" });
    expect(within(sheet).getByText("Model: gpt-test")).toBeInTheDocument();
    expect(screen.queryByRole("separator", { name: "Resize side panel" })).toBeNull();
    fireEvent.click(within(sheet).getByRole("button", { name: "Close" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  });

  // Decided in the SL.4 review: narrowing closes the column rather than popping a modal Sheet.
  test("narrowing the window closes an open column; no modal Sheet appears", () => {
    media = mockMatchMedia(true);
    render(<FrameHarness><AgentPage /></FrameHarness>);
    expect(screen.getByRole("complementary", { name: "Config" })).toBeInTheDocument();
    act(() => media.setWide(false));
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(screen.queryByTestId("side-panel")).toBeNull();
    expect(screen.getByRole("button", { name: "Config" })).toHaveAttribute("aria-pressed", "false");
  });

  test("defaultOpen does not open a Sheet on a phone", () => {
    media = mockMatchMedia(false);
    render(<FrameHarness><AgentPage defaultOpen /></FrameHarness>);
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(screen.getByRole("button", { name: "Config" })).toHaveAttribute("aria-pressed", "false");
  });

  test("hydrating on a phone: the first pass assumes wide, but defaultOpen still opens nothing", async () => {
    media = mockMatchMedia(false);
    const ui = <FrameHarness><AgentPage defaultOpen /></FrameHarness>;
    const container = document.createElement("div");
    container.innerHTML = renderToString(ui);
    document.body.appendChild(container);
    const errors = vi.spyOn(console, "error").mockImplementation(() => {});
    // React 19 reports some hydration mismatches through onRecoverableError, not console.error.
    const recoverable = vi.fn();
    let root: Root | undefined;
    await act(async () => {
      root = hydrateRoot(container, ui, { onRecoverableError: recoverable });
    });
    expect(document.querySelector('[role="dialog"]')).toBeNull();
    expect(container.querySelector('[data-testid="side-panel"]')).toBeNull();
    expect(errors).not.toHaveBeenCalled();
    expect(recoverable).not.toHaveBeenCalled();
    act(() => root?.unmount());
    container.remove();
  });
});

describe("SidePanel outside the frame", () => {
  test("renders its content in place and the controls are inert", () => {
    render(<AgentPage />);
    expect(screen.getByRole("complementary", { name: "Config" })).toHaveTextContent("Model: gpt-test");
    expect(screen.getByRole("button", { name: "Config" })).toBeDisabled();
  });
});

describe("frame storage", () => {
  test("clamps the panel width between 360 px and half the window", () => {
    expect(clampSidePanelWidth(100, 1600)).toBe(360);
    expect(clampSidePanelWidth(1000, 1600)).toBe(800);
    expect(clampSidePanelWidth(500, 1600)).toBe(500);
    expect(clampSidePanelWidth(Number.NaN, 1600)).toBe(SIDE_PANEL_DEFAULT_WIDTH);
    // A narrow window never pushes the maximum below the minimum.
    expect(sidePanelMaxWidth(500)).toBe(360);
  });

  test("malformed or blocked storage falls back to the defaults", () => {
    window.localStorage.setItem(SIDE_PANEL_STORAGE_KEY, "{not json");
    expect(readSidePanelWidth()).toBe(SIDE_PANEL_DEFAULT_WIDTH);
    window.localStorage.setItem(SIDE_PANEL_STORAGE_KEY, JSON.stringify({ width: "wide" }));
    expect(readSidePanelWidth()).toBe(SIDE_PANEL_DEFAULT_WIDTH);
    window.localStorage.setItem(SIDEBAR_STORAGE_KEY, "sideways");
    expect(readSidebarState()).toBe("expanded");
    const getItem = vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new Error("blocked");
    });
    expect(readSidePanelWidth()).toBe(SIDE_PANEL_DEFAULT_WIDTH);
    expect(readSidebarState()).toBe("expanded");
    getItem.mockRestore();
  });
});
