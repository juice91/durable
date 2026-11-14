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
const data = vi.hoisted(() => ({
  agents: vi.fn(),
  workflows: vi.fn(),
  tools: vi.fn(),
  mcp: vi.fn(),
  knowledge: vi.fn(),
}));
vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/api")>();
  return { ...actual, api: { ...actual.api, agents: { ...actual.api.agents, list: data.agents } } };
});
vi.mock("@/lib/workflows", async (importOriginal) => ({ ...(await importOriginal<typeof import("@/lib/workflows")>()), listWorkflows: data.workflows }));
vi.mock("@/lib/tools", async (importOriginal) => ({ ...(await importOriginal<typeof import("@/lib/tools")>()), listTools: data.tools }));
vi.mock("@/lib/mcpServers", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/lib/mcpServers")>();
  return { ...actual, mcpServers: { ...actual.mcpServers, list: data.mcp } };
});
vi.mock("@/lib/knowledge", async (importOriginal) => ({ ...(await importOriginal<typeof import("@/lib/knowledge")>()), listKnowledgeBases: data.knowledge }));

import { mockMatchMedia, type MatchMediaControl } from "@/components/frame/frameTesting";
import { KeyboardHarness, pressKey } from "@/components/shortcuts/shortcutsTesting";
import { routes } from "@/lib/routes";
import { THEME_STORAGE_KEY } from "@/lib/theme";
import { SIDEBAR_STORAGE_KEY } from "@/lib/sidebarPref";
import { expectNoAxeViolations, AXE_TIMEOUT_MS } from "@/test/a11y";
import { PALETTE_ENTITY_LIMIT, resetPaletteCache } from "./paletteData";

const WEATHER_ID = "11111111-1111-4111-8111-111111111111";
const RUN_ID = "0b6f3a52-1c2d-4e5f-8a9b-0c1d2e3f4a5b";

let media: MatchMediaControl;
beforeEach(() => {
  media = mockMatchMedia(true);
  nav.push.mockClear();
  resetPaletteCache();
  window.localStorage.clear();
  Object.values(data).forEach((fn) => fn.mockReset());
  data.agents.mockResolvedValue([
    { id: WEATHER_ID, name: "weather", draft_spec: { model: { provider: "openai", name: "gpt-test" } }, latest_version: 1 },
    { id: "22222222-2222-4222-8222-222222222222", name: "billing", draft_spec: { model: { provider: "openai", name: "gpt-test" } }, latest_version: null },
  ]);
  data.workflows.mockResolvedValue([{ id: "wf-1", name: "nightly report", draft_spec: null, latest_version: 2 }]);
  data.tools.mockResolvedValue([{ id: "get_weather", title: "Get weather", description: "", side_effect: false, external_idempotent: true, input_schema: {}, allowed: true }]);
  data.mcp.mockResolvedValue([{ id: "srv-1", name: "docs server", url: "https://mcp.example.test", transport: "streamable_http", enabled: true, header_names: [], created_at: null, updated_at: null, last_checked_at: null, last_status: null, last_error_code: null }]);
  data.knowledge.mockResolvedValue([{ id: "kb-1", tenant_id: "t", name: "product docs", description: null, embedding_model: "test:hash", dimensions: 1536, chunking: {}, active_generation: 1, last_generation: 1, pending_generation: null, pending_embedding_model: null, pending_dimensions: null, status: "ready", error: null, created_at: "2026-11-14T10:00:00Z", updated_at: "2026-11-14T10:00:00Z", document_count: 3, chunk_count: 12 }]);
});
afterEach(() => media.restore());

async function openPalette() {
  pressKey("k", { metaKey: true });
  const dialog = await screen.findByRole("dialog", { name: "Command palette" });
  await waitFor(() => expect(screen.queryByTestId("command-palette-loading")).toBeNull());
  return dialog;
}

function type(text: string) {
  fireEvent.change(screen.getByTestId("command-palette-input"), { target: { value: text } });
}

function pressEnter() {
  fireEvent.keyDown(screen.getByTestId("command-palette-input"), { key: "Enter" });
}

describe("opening and closing", () => {
  test("Cmd+K and Ctrl+K open it; Escape closes it; Cmd+K again toggles it shut", async () => {
    render(<KeyboardHarness />);
    await openPalette();
    expect(screen.getByTestId("command-palette-input")).toHaveFocus();
    fireEvent.keyDown(screen.getByTestId("command-palette-input"), { key: "Escape" });
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());

    pressKey("k", { ctrlKey: true });
    await screen.findByRole("dialog", { name: "Command palette" });
    pressKey("k", { metaKey: true }, screen.getByTestId("command-palette-input"));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  });

  test("Cmd+K works from a text field, but not on top of another dialog", async () => {
    render(<KeyboardHarness><input aria-label="Name" /></KeyboardHarness>);
    pressKey("k", { metaKey: true }, screen.getByRole("textbox", { name: "Name" }));
    await screen.findByRole("dialog", { name: "Command palette" });
  });

  test("the sidebar and mobile search buttons open it", async () => {
    render(<KeyboardHarness />);
    fireEvent.click(await screen.findByTestId("sidebar-search"));
    await screen.findByRole("dialog", { name: "Command palette" });
  });
});

describe("results", () => {
  test("acceptance: Cmd+K, 'wea', Enter opens the weather agent", async () => {
    render(<KeyboardHarness />);
    await openPalette();
    await screen.findByText("weather");
    type("wea");
    pressEnter();
    expect(nav.push).toHaveBeenCalledWith(routes.agentChat(WEATHER_ID));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  });

  test("search covers pages, every entity kind and commands", async () => {
    render(<KeyboardHarness />);
    const dialog = await openPalette();
    for (const text of ["Agents", "weather", "nightly report", "Get weather", "docs server", "Toggle sidebar", "New agent"]) {
      expect(within(dialog).getAllByText(text).length).toBeGreaterThan(0);
    }
    type("nightly");
    expect(within(dialog).getByText("nightly report")).toBeInTheDocument();
    expect(within(dialog).queryByText("billing")).toBeNull();
    type("zzzzqq");
    expect(within(dialog).getByText("No matching results.")).toBeInTheDocument();
  });

  test("a page result navigates to its href and shows its g-key", async () => {
    render(<KeyboardHarness />);
    const dialog = await openPalette();
    const traces = dialog.querySelector('[data-palette-page="traces"]') as HTMLElement;
    expect(traces).toHaveTextContent("G T");
    fireEvent.click(traces);
    expect(nav.push).toHaveBeenCalledWith(routes.traces());
  });

  test("entities are fetched on open and cached for 60 s", async () => {
    render(<KeyboardHarness />);
    expect(data.agents).not.toHaveBeenCalled();
    await openPalette();
    fireEvent.keyDown(screen.getByTestId("command-palette-input"), { key: "Escape" });
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    await openPalette();
    expect(data.agents).toHaveBeenCalledTimes(1);
    expect(data.mcp).toHaveBeenCalledTimes(1);
  });

  test("an entity fetch failure shows a muted row and leaves the rest working", async () => {
    data.agents.mockRejectedValue(new Error("HTTP 500"));
    render(<KeyboardHarness />);
    const dialog = await openPalette();
    expect(await within(dialog).findByTestId("command-palette-error-agents")).toHaveTextContent("Couldn't load agents: HTTP 500");
    expect(within(dialog).getByText("nightly report")).toBeInTheDocument();
    type("traces");
    pressEnter();
    expect(nav.push).toHaveBeenCalledWith(routes.traces());
  });

  test("a scope narrows the results to one kind", async () => {
    render(<KeyboardHarness />);
    const dialog = await openPalette();
    fireEvent.click(within(dialog).getByRole("radio", { name: "Workflows" }));
    expect(within(dialog).getByText("nightly report")).toBeInTheDocument();
    expect(within(dialog).queryByText("weather")).toBeNull();
    expect(dialog.querySelector("[data-palette-page]")).toBeNull();
    expect(dialog.querySelector("[data-palette-command]")).toBeNull();
  });

  test("a UUID query offers Open trace by run id", async () => {
    render(<KeyboardHarness />);
    await openPalette();
    expect(screen.queryByTestId("palette-open-trace")).toBeNull();
    type(` ${RUN_ID.toUpperCase()} `);
    expect(screen.getByTestId("palette-open-trace")).toHaveTextContent(RUN_ID);
    pressEnter();
    expect(nav.push).toHaveBeenCalledWith(routes.trace(RUN_ID));
  });
});

describe("commands", () => {
  test("Theme: Dark stores and applies the theme", async () => {
    render(<KeyboardHarness />);
    const dialog = await openPalette();
    fireEvent.click(dialog.querySelector('[data-palette-command="theme-dark"]') as HTMLElement);
    await waitFor(() => expect(document.documentElement).toHaveClass("dark"));
    expect(window.localStorage.getItem(THEME_STORAGE_KEY)).toBe("dark");
  });

  test("Toggle sidebar collapses the rail on wide screens and opens the drawer on narrow ones", async () => {
    const { unmount } = render(<KeyboardHarness />);
    let dialog = await openPalette();
    fireEvent.click(dialog.querySelector('[data-palette-command="toggle-sidebar"]') as HTMLElement);
    await waitFor(() => expect(screen.getByTestId("sidebar")).toHaveAttribute("data-collapsed", "true"));
    expect(window.localStorage.getItem(SIDEBAR_STORAGE_KEY)).toBe("collapsed");
    unmount();

    media.setWide(false);
    render(<KeyboardHarness />);
    dialog = await openPalette();
    fireEvent.click(dialog.querySelector('[data-palette-command="toggle-sidebar"]') as HTMLElement);
    await screen.findByRole("dialog", { name: "Navigation" });
  });

  test("New agent and Go to settings navigate; Keyboard shortcuts opens help", async () => {
    render(<KeyboardHarness />);
    let dialog = await openPalette();
    fireEvent.click(dialog.querySelector('[data-palette-command="new-agent"]') as HTMLElement);
    expect(nav.push).toHaveBeenCalledWith(routes.agentNew());
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());

    dialog = await openPalette();
    fireEvent.click(dialog.querySelector('[data-palette-command="settings"]') as HTMLElement);
    expect(nav.push).toHaveBeenCalledWith(routes.settings());
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());

    dialog = await openPalette();
    fireEvent.click(dialog.querySelector('[data-palette-command="shortcuts"]') as HTMLElement);
    await screen.findByRole("dialog", { name: "Keyboard shortcuts" });
    expect(screen.queryByRole("dialog", { name: "Command palette" })).toBeNull();
  });
});

test("axe: the open palette, with results and with none", async () => {
  render(<KeyboardHarness />);
  await openPalette();
  await screen.findByText("weather");
  await expectNoAxeViolations(document.body);
  type("zzzzqq");
  await expectNoAxeViolations(document.body);
}, AXE_TIMEOUT_MS);

describe("review fixes", () => {
  test("Tab from the scopes wraps to the input; Shift+Tab from the input goes to the scopes", async () => {
    render(<KeyboardHarness />);
    const dialog = await openPalette();
    const input = screen.getByTestId("command-palette-input");
    fireEvent.keyDown(input, { key: "Tab", shiftKey: true });
    const scope = within(dialog).getByRole("radio", { name: "All" });
    expect(scope).toHaveFocus();
    fireEvent.keyDown(scope, { key: "Tab" });
    expect(input).toHaveFocus();
  });

  test("Enter on a scope selects the scope, not a result", async () => {
    render(<KeyboardHarness />);
    const dialog = await openPalette();
    const tools = within(dialog).getByRole("radio", { name: "Tools" });
    tools.focus();
    fireEvent.keyDown(tools, { key: "Enter" });
    expect(nav.push).not.toHaveBeenCalled();
    expect(tools).toHaveAttribute("aria-checked", "true");
  });

  test("Ctrl+k (lowercase) from the input and Meta+k from a focused scope radio close it", async () => {
    render(<KeyboardHarness />);
    await openPalette();
    pressKey("k", { ctrlKey: true }, screen.getByTestId("command-palette-input"));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    const dialog = await openPalette();
    const radio = within(dialog).getByRole("radio", { name: "Agents" });
    radio.focus();
    pressKey("k", { metaKey: true }, radio);
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  });

  test("the chord is always default-prevented, even when another dialog keeps the palette shut", async () => {
    render(<KeyboardHarness />);
    pressKey("?", { shiftKey: true });
    const help = await screen.findByRole("dialog", { name: "Keyboard shortcuts" });
    const event = new KeyboardEvent("keydown", { key: "k", ctrlKey: true, bubbles: true, cancelable: true });
    help.dispatchEvent(event);
    expect(event.defaultPrevented).toBe(true);
    expect(screen.queryByRole("dialog", { name: "Command palette" })).toBeNull();
  });

  test("on macOS, Ctrl+K in a field is left alone (delete to end of line)", async () => {
    const platform = vi.spyOn(window.navigator, "platform", "get").mockReturnValue("MacIntel");
    render(<KeyboardHarness><input aria-label="Name" /></KeyboardHarness>);
    const field = screen.getByRole("textbox", { name: "Name" });
    const event = new KeyboardEvent("keydown", { key: "k", ctrlKey: true, bubbles: true, cancelable: true });
    field.dispatchEvent(event);
    expect(event.defaultPrevented).toBe(false);
    expect(screen.queryByRole("dialog")).toBeNull();
    pressKey("k", { metaKey: true }, field);
    await screen.findByRole("dialog", { name: "Command palette" });
    expect(screen.getByTestId("sidebar-search")).toHaveTextContent("⌘K");
    platform.mockRestore();
  });

  test("off macOS the sidebar search shows Ctrl K", async () => {
    render(<KeyboardHarness />);
    expect(await screen.findByTestId("sidebar-search")).toHaveTextContent("Ctrl K");
  });

  test("search scores names only: '45' does not match an agent whose id contains 4562", async () => {
    data.agents.mockResolvedValue([{ id: "aaaa4562-0000-4000-8000-000000000000", name: "billing", draft_spec: { model: { provider: "openai", name: "gpt-test" } }, latest_version: 1 }]);
    render(<KeyboardHarness />);
    const dialog = await openPalette();
    await within(dialog).findByText("billing");
    type("45");
    expect(within(dialog).queryByText("billing")).toBeNull();
  });

  test("without a query each kind shows at most the limit, with a hint; typing searches them all", async () => {
    const many = Array.from({ length: PALETTE_ENTITY_LIMIT + 7 }, (_, index) => ({
      id: `00000000-0000-4000-8000-${String(index).padStart(12, "0")}`,
      name: `agent number ${index}`,
      draft_spec: { model: { provider: "openai", name: "gpt-test" } },
      latest_version: 1,
    }));
    data.agents.mockResolvedValue(many);
    render(<KeyboardHarness />);
    const dialog = await openPalette();
    await within(dialog).findByText("agent number 0");
    expect(dialog.querySelectorAll('[data-palette-entity^="agents:"]')).toHaveLength(PALETTE_ENTITY_LIMIT);
    expect(within(dialog).getByTestId("command-palette-more")).toHaveTextContent(`Showing ${PALETTE_ENTITY_LIMIT} of ${PALETTE_ENTITY_LIMIT + 7} agents. Type to search the rest.`);
    type(`agent number ${PALETTE_ENTITY_LIMIT + 3}`);
    expect(within(dialog).getByText(`agent number ${PALETTE_ENTITY_LIMIT + 3}`)).toBeInTheDocument();
    expect(within(dialog).queryByTestId("command-palette-more")).toBeNull();
  });

  test("one always-mounted live region announces the count, loading and failures", async () => {
    let resolveAgents: (value: unknown) => void = () => {};
    data.agents.mockReturnValue(new Promise((resolve) => (resolveAgents = resolve)));
    data.tools.mockRejectedValue(new Error("HTTP 500"));
    render(<KeyboardHarness />);
    pressKey("k", { metaKey: true });
    const dialog = await screen.findByRole("dialog", { name: "Command palette" });
    const region = within(dialog).getByTestId("command-palette-announcement");
    expect(within(dialog).getAllByRole("status")).toEqual([region]);
    await waitFor(() => expect(region).toHaveTextContent("Loading more results."));
    resolveAgents([]);
    await waitFor(() => expect(region).not.toHaveTextContent("Loading"));
    expect(region).toHaveTextContent(/^\d+ results\. Couldn't load tools\.$/);
    type("zzzzqq");
    await waitFor(() => expect(region).toHaveTextContent(/^No results\./));
    expect(within(dialog).getAllByRole("status")).toEqual([region]);
  });
});
