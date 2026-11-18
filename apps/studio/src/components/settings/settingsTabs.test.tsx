import React from "react";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const nav = vi.hoisted(() => ({ search: "", push: vi.fn(), redirect: vi.fn() }));
vi.mock("next/navigation", () => ({
  usePathname: () => "/settings",
  useParams: () => ({}),
  useRouter: () => ({ push: nav.push, replace: vi.fn(), back: vi.fn(), prefetch: vi.fn() }),
  useSearchParams: () => new URLSearchParams(nav.search),
  redirect: (url: string) => nav.redirect(url),
}));
vi.mock("./SecretsSection", () => ({ SecretsSection: () => <section data-testid="secrets-stub"><h2>Secrets</h2></section> }));
vi.mock("./ConnectionsSection", () => ({ ConnectionsSection: () => <section data-testid="connections-stub"><h2>Connections</h2></section> }));
vi.mock("@/components/apiKeys/ApiKeysSection", () => ({ ApiKeysSection: () => <section data-testid="api-keys-stub"><h2>API keys</h2></section> }));
vi.mock("./ProvidersSettings", () => ({ ProvidersSettings: () => <div data-testid="providers-settings" /> }));

import SettingsPage from "@/app/settings/page";
import ProvidersRedirect from "@/app/settings/providers/page";
import { FrameHarness, mockMatchMedia, type MatchMediaControl } from "@/components/frame/frameTesting";
import { routes } from "@/lib/routes";
import { SETTINGS_TABS } from "@/lib/routes/settings";

let media: MatchMediaControl;
beforeEach(() => {
  media = mockMatchMedia(true);
  nav.search = "";
  nav.push.mockClear();
  nav.redirect.mockClear();
});
afterEach(() => {
  cleanup();
  media.restore();
});

const page = () => render(<FrameHarness><SettingsPage /></FrameHarness>);

describe("Settings tabs", () => {
  it("lists the nine tabs in order under one h1, Appearance selected by default", () => {
    page();
    const tabs = screen.getAllByRole("tab");
    expect(tabs.map((t) => t.textContent)).toEqual(["Appearance", "Chat", "Shortcuts", "Providers", "Secrets", "Connections", "API keys", "Browser data", "About"]);
    expect(screen.getAllByRole("heading", { level: 1 })).toHaveLength(1);
    expect(screen.getByRole("tab", { name: "Appearance" })).toHaveAttribute("aria-selected", "true");
    expect(screen.getByTestId("settings-appearance")).toBeInTheDocument();
    expect(screen.queryByTestId("settings-about")).not.toBeInTheDocument();
  });

  it.each([
    ["shortcuts", "settings-shortcuts"],
    ["providers", "settings-providers"],
    ["secrets", "secrets-stub"],
    ["connections", "connections-stub"],
    ["api-keys", "api-keys-stub"],
    ["browser-data", "settings-browser-data"],
    ["about", "settings-about"],
    ["chat", "settings-chat"],
  ])("?tab=%s selects that tab and shows only its section", (tab, testId) => {
    nav.search = `tab=${tab}`;
    page();
    expect(screen.getByTestId(testId)).toBeInTheDocument();
    expect(screen.getAllByRole("tab").filter((t) => t.getAttribute("aria-selected") === "true")).toHaveLength(1);
    expect(screen.queryByTestId("settings-appearance")).not.toBeInTheDocument();
  });

  it("an unknown ?tab= falls back to Appearance", () => {
    nav.search = "tab=nope";
    page();
    expect(screen.getByRole("tab", { name: "Appearance" })).toHaveAttribute("aria-selected", "true");
  });

  it("choosing a tab pushes its URL, so back returns to the previous tab", () => {
    page();
    fireEvent.mouseDown(screen.getByRole("tab", { name: "Secrets" }));
    fireEvent.focus(screen.getByRole("tab", { name: "Secrets" }));
    expect(nav.push).toHaveBeenCalledWith("/settings?tab=secrets", { scroll: false });
  });

  it("the route helpers name the tabs", () => {
    expect(SETTINGS_TABS).toHaveLength(9);
    expect(routes.settingsTab("connections")).toBe("/settings?tab=connections");
    expect(routes.settingsProviders()).toBe("/settings?tab=providers");
    expect(routes.settingsApiKeys()).toBe("/settings?tab=api-keys");
  });

  it("/settings/providers redirects to the Providers tab", () => {
    ProvidersRedirect();
    expect(nav.redirect).toHaveBeenCalledWith("/settings?tab=providers");
  });
});
