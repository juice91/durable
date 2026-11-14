import React from "react";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { renderToString } from "react-dom/server";
import { hydrateRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";

vi.mock("next/navigation", () => ({
  usePathname: () => "/settings",
  useParams: () => ({}),
  useRouter: () => ({ push: vi.fn(), replace: vi.fn(), back: vi.fn(), prefetch: vi.fn() }),
  useSearchParams: () => new URLSearchParams(),
}));

import SettingsPage from "@/app/settings/page";
import { FrameHarness, mockMatchMedia, type MatchMediaControl } from "@/components/frame/frameTesting";
import { SIDE_PANEL_STORAGE_KEY } from "@/components/frame/frameStorage";
import { BASE_URL } from "@/lib/api";
import { SAVED_CONTEXT_PREFIX } from "@/lib/requestContext";
import { SIDEBAR_ATTRIBUTE, SIDEBAR_STORAGE_KEY } from "@/lib/sidebarPref";
import { THEME_STORAGE_KEY } from "@/lib/theme";
import { expectNoAxeViolations, AXE_TIMEOUT_MS, PAGE_RULES } from "@/test/a11y";
import { AGENT_LAYOUT_PREFIX, BROWSER_DATA, clearBrowserData, keysFor, LAST_AGENT_KEY } from "./browserData";

const LAYOUT_KEYS = [SIDEBAR_STORAGE_KEY, SIDE_PANEL_STORAGE_KEY, `${AGENT_LAYOUT_PREFIX}a1`, `${AGENT_LAYOUT_PREFIX}a2`, LAST_AGENT_KEY];
const PRESET_KEYS = [`${SAVED_CONTEXT_PREFIX}cabc:a1`, `${SAVED_CONTEXT_PREFIX}cabc:a2`];
const OTHER_KEYS = [THEME_STORAGE_KEY, "unrelated.key", "studio.something-else"];

function seed() {
  for (const key of [...LAYOUT_KEYS, ...PRESET_KEYS]) window.localStorage.setItem(key, key === SIDEBAR_STORAGE_KEY ? "collapsed" : "{}");
  window.localStorage.setItem(THEME_STORAGE_KEY, "light");
  window.localStorage.setItem("unrelated.key", "1");
  window.localStorage.setItem("studio.something-else", "1");
}

let media: MatchMediaControl;
beforeEach(() => {
  media = mockMatchMedia(true);
  // The root layout sets lang="en" on <html>; jsdom's document has none.
  document.documentElement.lang = "en";
  window.localStorage.clear();
  document.documentElement.removeAttribute(SIDEBAR_ATTRIBUTE);
  document.documentElement.classList.remove("dark");
});
afterEach(() => media.restore());

describe("browserData", () => {
  test("keysFor finds exact and prefixed keys", () => {
    seed();
    const agentLayout = BROWSER_DATA.find((entry) => entry.id === "agent-layout")!;
    expect(keysFor(agentLayout)).toEqual([`${AGENT_LAYOUT_PREFIX}a1`, `${AGENT_LAYOUT_PREFIX}a2`]);
  });

  test.each([
    ["layout", LAYOUT_KEYS],
    ["presets", PRESET_KEYS],
  ] as const)("clearing %s removes exactly its keys", (group, keys) => {
    seed();
    const removeItem = vi.spyOn(Storage.prototype, "removeItem");
    const result = clearBrowserData(group);
    expect(result?.failed).toEqual([]);
    expect([...(result?.removed ?? [])].sort()).toEqual([...keys].sort());
    expect(removeItem.mock.calls.map(([key]) => key).sort()).toEqual([...keys].sort());
    for (const key of OTHER_KEYS) expect(window.localStorage.getItem(key)).not.toBeNull();
    removeItem.mockRestore();
  });

  test("removeItem throwing on the 2nd key: stops there and reports the partial result", () => {
    seed();
    const real = Storage.prototype.removeItem;
    let calls = 0;
    const removeItem = vi.spyOn(Storage.prototype, "removeItem").mockImplementation(function (this: Storage, key: string) {
      calls += 1;
      if (calls === 2) throw new Error("SecurityError");
      real.call(this, key);
    });
    const result = clearBrowserData("presets");
    expect(result?.removed).toEqual([PRESET_KEYS[0]]);
    expect(result?.failed).toEqual([PRESET_KEYS[1]]);
    expect(window.localStorage.getItem(PRESET_KEYS[0])).toBeNull();
    expect(window.localStorage.getItem(PRESET_KEYS[1])).not.toBeNull();
    removeItem.mockRestore();
  });

  test("throwing storage clears nothing and says so", () => {
    const broken = { get length() { throw new Error("blocked"); } } as unknown as Storage;
    expect(clearBrowserData("layout", null)).toBeNull();
    expect(keysFor(BROWSER_DATA[3], broken)).toEqual([]);
  });
});

describe("Settings page", () => {
  test("the four sections render under the Settings h1", () => {
    render(<FrameHarness><SettingsPage /></FrameHarness>);
    expect(screen.getByRole("heading", { level: 1, name: "Settings" })).toBeInTheDocument();
    for (const name of ["Appearance", "Keyboard shortcuts", "Data in this browser", "About"]) {
      expect(screen.getByRole("heading", { level: 2, name })).toBeInTheDocument();
    }
    expect(screen.getByTestId("settings-api-base")).toHaveTextContent(BASE_URL);
    expect(within(screen.getByTestId("settings-shortcuts")).getByTestId("shortcut-table")).toBeInTheDocument();
  });

  test("the theme control changes the class and storage", async () => {
    render(<FrameHarness><SettingsPage /></FrameHarness>);
    const control = within(screen.getByTestId("settings-theme"));
    fireEvent.click(control.getByRole("radio", { name: "Dark" }));
    await waitFor(() => expect(document.documentElement).toHaveClass("dark"));
    expect(window.localStorage.getItem(THEME_STORAGE_KEY)).toBe("dark");
    fireEvent.click(control.getByRole("radio", { name: "Light" }));
    await waitFor(() => expect(document.documentElement).not.toHaveClass("dark"));
    expect(window.localStorage.getItem(THEME_STORAGE_KEY)).toBe("light");
  });

  test("Clear layout preferences: confirm, then exactly its keys go, with a toast and an inline status", async () => {
    seed();
    render(<FrameHarness><SettingsPage /></FrameHarness>);
    await waitFor(() => expect(screen.getByTestId("sidebar")).toHaveAttribute("data-collapsed", "true"));
    expect(screen.getByTestId("browser-data-table").querySelector('[data-browser-data="agent-layout"]')).toHaveTextContent("2 saved");

    fireEvent.click(screen.getByTestId("settings-clear-layout"));
    const dialog = await screen.findByRole("dialog", { name: "Clear layout preferences?" });
    fireEvent.click(within(dialog).getByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(window.localStorage.getItem(SIDEBAR_STORAGE_KEY)).toBe("collapsed");

    fireEvent.click(screen.getByTestId("settings-clear-layout"));
    fireEvent.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "Clear" }));
    expect(await screen.findByTestId("settings-clear-status")).toHaveTextContent("Layout preferences cleared (5 items).");
    expect(await screen.findByText("Layout preferences cleared")).toBeInTheDocument();
    for (const key of LAYOUT_KEYS) expect(window.localStorage.getItem(key)).toBeNull();
    for (const key of [...PRESET_KEYS, ...OTHER_KEYS]) expect(window.localStorage.getItem(key)).not.toBeNull();
    // The sidebar shows its default at once, without writing storage back.
    await waitFor(() => expect(screen.getByTestId("sidebar")).toHaveAttribute("data-collapsed", "false"));
    expect(window.localStorage.getItem(SIDEBAR_STORAGE_KEY)).toBeNull();
    expect(screen.getByTestId("browser-data-table").querySelector('[data-browser-data="agent-layout"]')).toHaveTextContent("Not set");
  });

  test("Clear request-context presets removes only the presets", async () => {
    seed();
    render(<FrameHarness><SettingsPage /></FrameHarness>);
    fireEvent.click(screen.getByTestId("settings-clear-presets"));
    fireEvent.click(within(await screen.findByRole("dialog", { name: "Clear request-context presets?" })).getByRole("button", { name: "Clear" }));
    expect(await screen.findByTestId("settings-clear-status")).toHaveTextContent("Request-context presets cleared (2 items).");
    for (const key of PRESET_KEYS) expect(window.localStorage.getItem(key)).toBeNull();
    for (const key of [...LAYOUT_KEYS, ...OTHER_KEYS]) expect(window.localStorage.getItem(key)).not.toBeNull();
  });

  test("a partial clear says how much was removed", async () => {
    seed();
    const real = Storage.prototype.removeItem;
    let calls = 0;
    const removeItem = vi.spyOn(Storage.prototype, "removeItem").mockImplementation(function (this: Storage, key: string) {
      calls += 1;
      if (calls === 2) throw new Error("SecurityError");
      real.call(this, key);
    });
    render(<FrameHarness><SettingsPage /></FrameHarness>);
    fireEvent.click(screen.getByTestId("settings-clear-presets"));
    fireEvent.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "Clear" }));
    expect(await screen.findByTestId("settings-clear-status")).toHaveTextContent("Cleared 1 of 2 items; the browser refused to remove the rest.");
    removeItem.mockRestore();
  });

  test("clearing when nothing is stored says so", async () => {
    render(<FrameHarness><SettingsPage /></FrameHarness>);
    fireEvent.click(screen.getByTestId("settings-clear-presets"));
    fireEvent.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "Clear" }));
    expect(await screen.findByTestId("settings-clear-status")).toHaveTextContent("There were no request-context presets to clear.");
  });

  test("axe: the page and the confirm dialog", async () => {
    render(<FrameHarness><SettingsPage /></FrameHarness>);
    await expectNoAxeViolations(document, { rules: PAGE_RULES });
    fireEvent.click(screen.getByTestId("settings-clear-layout"));
    await screen.findByRole("dialog");
    await expectNoAxeViolations(document.body);
  }, AXE_TIMEOUT_MS);

  test("hydrates without mismatches (storage is read after mount)", async () => {
    seed();
    const ui = <FrameHarness><SettingsPage /></FrameHarness>;
    const container = document.createElement("div");
    container.innerHTML = renderToString(ui);
    document.body.appendChild(container);
    const errors = vi.spyOn(console, "error").mockImplementation(() => {});
    const recoverable = vi.fn();
    let root: Root | undefined;
    await act(async () => {
      root = hydrateRoot(container, ui, { onRecoverableError: recoverable });
    });
    expect(errors).not.toHaveBeenCalled();
    expect(recoverable).not.toHaveBeenCalled();
    expect(container.querySelector('[data-browser-data="request-context"]')).toHaveTextContent("2 saved");
    errors.mockRestore();
    act(() => root?.unmount());
    container.remove();
  });
});
