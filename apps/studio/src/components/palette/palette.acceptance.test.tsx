import React from "react";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";

const nav = vi.hoisted(() => ({ push: vi.fn() }));
vi.mock("next/navigation", () => ({
  usePathname: () => "/agents",
  useParams: () => ({}),
  useRouter: () => ({ push: nav.push, replace: vi.fn(), back: vi.fn(), prefetch: vi.fn() }),
  useSearchParams: () => new URLSearchParams(),
}));
const data = vi.hoisted(() => ({ agents: vi.fn(), workflows: vi.fn(), tools: vi.fn(), mcp: vi.fn(), knowledge: vi.fn() }));
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
import { useCreateAction } from "@/components/shortcuts/ShortcutsProvider";
import { KeyboardHarness, pressKey, pressKeys } from "@/components/shortcuts/shortcutsTesting";
import { globalBindings, G_PREFIX, SEQUENCE_TIMEOUT_MS, shortcutSections } from "@/lib/shortcuts";
import SettingsPage from "@/app/settings/page";
import { FrameHarness } from "@/components/frame/frameTesting";
import { SIDE_PANEL_STORAGE_KEY } from "@/components/frame/frameStorage";
import { AGENT_LAYOUT_PREFIX, BROWSER_DATA, LAST_AGENT_KEY } from "@/components/settings/browserData";
import { SAVED_CONTEXT_PREFIX } from "@/lib/requestContext";
import { SIDEBAR_STORAGE_KEY } from "@/lib/sidebarPref";
import { allNavItems } from "@/lib/nav";
import { routes } from "@/lib/routes";
import { THEME_STORAGE_KEY } from "@/lib/theme";
import { expectNoAxeViolations, AXE_TIMEOUT_MS } from "@/test/a11y";
import { resetPaletteCache } from "./paletteData";

const WEATHER_ID = "11111111-1111-4111-8111-111111111111";
const BILLING_ID = "22222222-2222-4222-8222-222222222222";
const RUN_ID = "0b6f3a52-1c2d-4e5f-8a9b-0c1d2e3f4a5b";
const SECRET = "sk-super-secret-value";

let media: MatchMediaControl;
beforeEach(() => {
  media = mockMatchMedia(true);
  nav.push.mockClear();
  resetPaletteCache();
  window.localStorage.clear();
  document.documentElement.classList.remove("dark");
  Object.values(data).forEach((fn) => fn.mockReset());
  data.agents.mockResolvedValue([
    { id: WEATHER_ID, name: "weather", draft_spec: { instructions: SECRET, model: { provider: "openai", name: "gpt-test" } }, latest_version: 1 },
    { id: BILLING_ID, name: "billing", draft_spec: null, latest_version: null },
  ]);
  data.workflows.mockResolvedValue([{ id: "wf-1", name: "nightly report", draft_spec: null, latest_version: 2 }]);
  data.tools.mockResolvedValue([{ id: "get_weather", title: "Get weather", description: SECRET, side_effect: false, external_idempotent: true, input_schema: {}, allowed: true }]);
  data.mcp.mockResolvedValue([{ id: "srv-1", name: "docs server", url: "https://mcp.example.test", transport: "streamable_http", enabled: true, header_names: ["X-Api-Key"], created_at: null, updated_at: null, last_checked_at: null, last_status: null, last_error_code: null }]);
  data.knowledge.mockResolvedValue([{ id: "kb-1", tenant_id: "t", name: "product docs", description: null, embedding_model: "test:hash", dimensions: 1536, chunking: {}, active_generation: 1, last_generation: 1, pending_generation: null, pending_embedding_model: null, pending_dimensions: null, status: "ready", error: null, created_at: "2026-11-14T10:00:00Z", updated_at: "2026-11-14T10:00:00Z", document_count: 3, chunk_count: 12 }]);
});
afterEach(() => {
  media.restore();
  vi.useRealTimers();
});

const input = () => screen.getByTestId("command-palette-input");
const type = (text: string) => fireEvent.change(input(), { target: { value: text } });
const key = (k: string) => fireEvent.keyDown(input(), { key: k });
const closed = () => waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());

async function openPalette(settle = true) {
  pressKey("k", { metaKey: true });
  const dialog = await screen.findByRole("dialog", { name: "Command palette" });
  if (settle) await waitFor(() => expect(screen.queryByTestId("command-palette-loading")).toBeNull());
  return dialog;
}

const kinds = (dialog: HTMLElement) => ({
  pages: dialog.querySelectorAll("[data-palette-page]").length,
  agents: dialog.querySelectorAll('[data-palette-entity^="agents:"]').length,
  workflows: dialog.querySelectorAll('[data-palette-entity^="workflows:"]').length,
  tools: dialog.querySelectorAll('[data-palette-entity^="tools:"]').length,
  mcp: dialog.querySelectorAll('[data-palette-entity^="mcp:"]').length,
  knowledge: dialog.querySelectorAll('[data-palette-entity^="knowledge:"]').length,
  commands: dialog.querySelectorAll("[data-palette-command]").length,
});

describe("scopes", () => {
  const PAGES = allNavItems.length;
  const EXPECTED: Record<string, Partial<ReturnType<typeof kinds>>> = {
    All: { pages: PAGES, agents: 2, workflows: 1, tools: 1, mcp: 1, knowledge: 1, commands: 7 },
    Pages: { pages: PAGES },
    Agents: { agents: 2 },
    Workflows: { workflows: 1 },
    Tools: { tools: 1 },
    "MCP servers": { mcp: 1 },
    Knowledge: { knowledge: 1 },
    Commands: { commands: 7 },
  };
  test.each(Object.entries(EXPECTED))("the %s scope shows only its own kind", async (name, expected) => {
    render(<KeyboardHarness />);
    const dialog = await openPalette();
    fireEvent.click(within(dialog).getByRole("radio", { name }));
    expect(kinds(dialog)).toEqual({ pages: 0, agents: 0, workflows: 0, tools: 0, mcp: 0, knowledge: 0, commands: 0, ...expected });
  });

  test("a scope still filters by the query, and switching back restores the rest", async () => {
    render(<KeyboardHarness />);
    const dialog = await openPalette();
    fireEvent.click(within(dialog).getByRole("radio", { name: "Agents" }));
    type("bill");
    expect(kinds(dialog)).toMatchObject({ agents: 1, pages: 0 });
    expect(dialog.querySelector("[data-palette-entity]")).toHaveAttribute("data-palette-entity", `agents:${BILLING_ID}`);
    fireEvent.click(within(dialog).getByRole("radio", { name: "All" }));
    type("");
    expect(kinds(dialog).pages).toBe(allNavItems.length);
    expect(dialog.querySelector(`[data-palette-entity="agents:${BILLING_ID}"]`)).not.toBeNull();
  });

  test("reopening resets the query and the scope", async () => {
    render(<KeyboardHarness />);
    let dialog = await openPalette();
    fireEvent.click(within(dialog).getByRole("radio", { name: "Tools" }));
    type("weather");
    key("Escape");
    await closed();
    dialog = await openPalette();
    expect(input()).toHaveValue("");
    expect(within(dialog).getByRole("radio", { name: "All" })).toBeChecked();
  });

  test("the UUID row is a Commands-scope row and only shows in All or Commands", async () => {
    render(<KeyboardHarness />);
    const dialog = await openPalette();
    for (const [name, shown] of [["Pages", false], ["Agents", false], ["Commands", true], ["All", true]] as const) {
      fireEvent.click(within(dialog).getByRole("radio", { name }));
      type(RUN_ID);
      expect(screen.queryByTestId("palette-open-trace") !== null).toBe(shown);
    }
  });
});

describe("fuzzy ranking", () => {
  test('"wea" ranks the weather agent first', async () => {
    render(<KeyboardHarness />);
    const dialog = await openPalette();
    type("wea");
    const first = dialog.querySelector("[cmdk-item]") as HTMLElement;
    expect(first).toHaveAttribute("data-palette-entity", `agents:${WEATHER_ID}`);
    expect(first).toHaveAttribute("aria-selected", "true");
    expect(within(dialog).getByRole("group", { name: "Results" })).toBeInTheDocument();
    expect(dialog.querySelectorAll("[cmdk-group]")).toHaveLength(1);
  });

  test('"trac" ranks Traces first', async () => {
    render(<KeyboardHarness />);
    const dialog = await openPalette();
    type("trac");
    expect(dialog.querySelector("[cmdk-item]")).toHaveAttribute("data-palette-page", "traces");
  });

  test("keywords reach pages: 'dashboard' finds Metrics, 'guardrails' finds Processors", async () => {
    render(<KeyboardHarness />);
    const dialog = await openPalette();
    type("dashboard");
    expect(dialog.querySelector("[cmdk-item]")).toHaveAttribute("data-palette-page", "metrics");
    type("guardrails");
    expect(dialog.querySelector("[cmdk-item]")).toHaveAttribute("data-palette-page", "processors");
  });

  test("without a query the results stay in sections, in registry order", async () => {
    render(<KeyboardHarness />);
    const dialog = await openPalette();
    const headings = Array.from(dialog.querySelectorAll("[cmdk-group-heading]")).map((node) => node.textContent);
    expect(headings).toEqual(["Pages", "Agents", "Workflows", "Tools", "MCP servers", "Knowledge", "Commands"]);
    const pages = Array.from(dialog.querySelectorAll("[data-palette-page]")).map((node) => node.getAttribute("data-palette-page"));
    expect(pages).toEqual(allNavItems.map((item) => item.id));
  });
});

describe("keyboard navigation and Enter", () => {
  test("the arrows move the selection (with wrap) and Enter opens the selected row", async () => {
    render(<KeyboardHarness />);
    const dialog = await openPalette();
    const selected = () => dialog.querySelector('[cmdk-item][aria-selected="true"]');
    expect(selected()).toHaveAttribute("data-palette-page", allNavItems[0].id);
    key("ArrowDown");
    expect(selected()).toHaveAttribute("data-palette-page", allNavItems[1].id);
    key("ArrowUp");
    key("ArrowUp");
    expect(selected()).toHaveAttribute("data-palette-command", "shortcuts");
    key("ArrowDown");
    expect(selected()).toHaveAttribute("data-palette-page", allNavItems[0].id);
    key("ArrowDown");
    key("Enter");
    expect(nav.push).toHaveBeenCalledTimes(1);
    expect(nav.push).toHaveBeenCalledWith(allNavItems[1].href);
    await closed();
  });

  test("Enter on a typed query opens the exact href of each row kind", async () => {
    const cases: [string, string][] = [
      ["wea", routes.agentChat(WEATHER_ID)],
      ["nightly", routes.workflow("wf-1")],
      ["Get weather", routes.tool("get_weather")],
      ["docs server", routes.mcpServer("srv-1")],
      ["New agent", routes.agentNew()],
      ["Go to settings", routes.settings()],
      [RUN_ID, routes.trace(RUN_ID)],
    ];
    for (const [text, href] of cases) {
      nav.push.mockClear();
      const { unmount } = render(<KeyboardHarness />);
      await openPalette();
      type(text);
      key("Enter");
      expect(nav.push, text).toHaveBeenCalledTimes(1);
      expect(nav.push, text).toHaveBeenCalledWith(href);
      await closed();
      unmount();
    }
  });

  test.each(allNavItems.map((item) => [item.id, item.href]))("the %s page row opens %s", async (id, href) => {
    render(<KeyboardHarness />);
    const dialog = await openPalette();
    fireEvent.click(dialog.querySelector(`[data-palette-page="${id}"]`) as HTMLElement);
    expect(nav.push.mock.calls).toEqual([[href]]);
  });

  test("the trace row carries the lower-cased UUID, however it was typed", async () => {
    render(<KeyboardHarness />);
    await openPalette();
    type(`  ${RUN_ID.toUpperCase()}  `);
    fireEvent.click(screen.getByTestId("palette-open-trace"));
    expect(nav.push.mock.calls).toEqual([[routes.trace(RUN_ID)]]);
    expect(routes.trace(RUN_ID)).toContain(RUN_ID);
  });

  test("Enter on a scope radio does not select a result; Escape there still closes", async () => {
    render(<KeyboardHarness />);
    const dialog = await openPalette();
    const radio = within(dialog).getByRole("radio", { name: "Tools" });
    act(() => radio.focus());
    fireEvent.keyDown(radio, { key: "Enter" });
    expect(nav.push).not.toHaveBeenCalled();
    fireEvent.keyDown(radio, { key: "Escape" });
    await closed();
  });
});

describe("fetch errors", () => {
  const SOURCES = [
    ["agents", "weather"],
    ["workflows", "nightly report"],
    ["tools", "Get weather"],
    ["mcp", "docs server"],
    ["knowledge", "product docs"],
  ] as const;

  test.each(SOURCES)("only the %s list failing leaves the others, the pages and the commands", async (failing) => {
    data[failing].mockRejectedValue(new Error("boom"));
    render(<KeyboardHarness />);
    const dialog = await openPalette();
    await within(dialog).findByTestId(`command-palette-error-${failing}`);
    expect(dialog.querySelectorAll('[data-testid^="command-palette-error-"]')).toHaveLength(1);
    const counts = kinds(dialog);
    expect(counts[failing]).toBe(0);
    expect(counts.pages).toBe(allNavItems.length);
    expect(counts.commands).toBe(7);
    for (const [other, label] of SOURCES) {
      if (other !== failing) expect(within(dialog).getAllByText(label).length).toBeGreaterThan(0);
    }
  });

  test("every list failing still leaves pages and commands usable, with a row per failure", async () => {
    Object.values(data).forEach((fn) => fn.mockRejectedValue(new Error("down")));
    render(<KeyboardHarness />);
    const dialog = await openPalette();
    await waitFor(() => expect(dialog.querySelectorAll('[data-testid^="command-palette-error-"]')).toHaveLength(5));
    type("settings");
    key("Enter");
    expect(nav.push).toHaveBeenCalledWith(routes.settings());
  });

  test("a non-Error rejection shows a generic message; the error row is muted text, announced by the one live region", async () => {
    data.tools.mockRejectedValue("nope");
    render(<KeyboardHarness />);
    const dialog = await openPalette();
    const row = await within(dialog).findByTestId("command-palette-error-tools");
    expect(row).toHaveTextContent("Couldn't load tools: Request failed");
    expect(row).not.toHaveAttribute("role");
    expect(row.className).toContain("text-muted-foreground");
    expect(within(dialog).getByTestId("command-palette-announcement")).toHaveTextContent("Couldn't load tools.");
  });

  test("a failed list is not cached: the next open retries and shows it", async () => {
    data.agents.mockRejectedValueOnce(new Error("HTTP 503"));
    render(<KeyboardHarness />);
    let dialog = await openPalette();
    await within(dialog).findByTestId("command-palette-error-agents");
    key("Escape");
    await closed();
    dialog = await openPalette();
    await waitFor(() => expect(dialog.querySelector(`[data-palette-entity="agents:${WEATHER_ID}"]`)).not.toBeNull());
    expect(within(dialog).queryByTestId("command-palette-error-agents")).toBeNull();
    expect(data.agents).toHaveBeenCalledTimes(2);
    expect(data.workflows).toHaveBeenCalledTimes(1);
  });

  test("while a list loads a Loading row shows and the pages are already searchable", async () => {
    data.workflows.mockReturnValue(new Promise(() => {}));
    render(<KeyboardHarness />);
    const dialog = await openPalette(false);
    expect(await within(dialog).findByTestId("command-palette-loading")).toHaveTextContent("Loading");
    expect(kinds(dialog).pages).toBe(allNavItems.length);
    fireEvent.click(within(dialog).getByRole("radio", { name: "Agents" }));
    await waitFor(() => expect(screen.queryByTestId("command-palette-loading")).toBeNull());
  });
});

describe("the 60 s cache", () => {
  async function reopen() {
    key("Escape");
    await closed();
    return openPalette();
  }
  const calls = () => [data.agents, data.workflows, data.tools, data.mcp, data.knowledge].map((fn) => fn.mock.calls.length);

  test("reopening within 60 s reuses every list; after 60 s it fetches again", async () => {
    vi.useFakeTimers({ toFake: ["Date"] });
    render(<KeyboardHarness />);
    await openPalette();
    expect(calls()).toEqual([1, 1, 1, 1, 1]);
    vi.advanceTimersByTime(59_000);
    await reopen();
    expect(calls()).toEqual([1, 1, 1, 1, 1]);
    vi.advanceTimersByTime(2_000);
    const dialog = await reopen();
    expect(calls()).toEqual([2, 2, 2, 2, 2]);
    expect(kinds(dialog)).toMatchObject({ agents: 2, workflows: 1, tools: 1, mcp: 1, knowledge: 1 });
  });

  test("cached lists show at once, with no Loading row, and new data replaces stale data after expiry", async () => {
    vi.useFakeTimers({ toFake: ["Date"] });
    render(<KeyboardHarness />);
    await openPalette();
    data.workflows.mockResolvedValue([{ id: "wf-2", name: "weekly digest", draft_spec: null, latest_version: 1 }]);
    vi.advanceTimersByTime(30_000);
    let dialog = await reopen();
    expect(within(dialog).queryByText("weekly digest")).toBeNull();
    vi.advanceTimersByTime(31_000);
    dialog = await reopen();
    await waitFor(() => expect(within(dialog).getByText("weekly digest")).toBeInTheDocument());
    expect(within(dialog).queryByText("nightly report")).toBeNull();
  });

  test("one failing list is retried on the next open while the cached ones are not", async () => {
    data.mcp.mockRejectedValueOnce(new Error("HTTP 500"));
    render(<KeyboardHarness />);
    const dialog = await openPalette();
    await within(dialog).findByTestId("command-palette-error-mcp");
    await reopen();
    expect(calls()).toEqual([1, 1, 1, 2, 1]);
  });
});

describe("focus, overlays and toggling", () => {
  test("closing returns focus to the element that had it", async () => {
    render(<KeyboardHarness><button type="button">Opener</button></KeyboardHarness>);
    const opener = screen.getByRole("button", { name: "Opener" });
    act(() => opener.focus());
    pressKey("k", { ctrlKey: true }, opener);
    await screen.findByRole("dialog", { name: "Command palette" });
    expect(input()).toHaveFocus();
    key("Escape");
    await closed();
    expect(opener).toHaveFocus();
  });

  test("it does not open on top of the help dialog, and stays shut after it closes", async () => {
    render(<KeyboardHarness />);
    pressKey("?", { shiftKey: true });
    const help = await screen.findByRole("dialog", { name: "Keyboard shortcuts" });
    pressKey("k", { metaKey: true }, help);
    pressKey("k", { ctrlKey: true }, help);
    expect(screen.queryByRole("dialog", { name: "Command palette" })).toBeNull();
    expect(screen.getAllByRole("dialog")).toHaveLength(1);
    pressKey("Escape", {}, help);
    await closed();
    expect(data.agents).not.toHaveBeenCalled();
  });

  test("Cmd+K and Ctrl+K toggle repeatedly, and Shift or Alt variants do nothing", async () => {
    render(<KeyboardHarness />);
    pressKey("k", { metaKey: true, shiftKey: true });
    pressKey("k", { ctrlKey: true, altKey: true });
    pressKey("k");
    expect(screen.queryByRole("dialog")).toBeNull();
    for (const mod of [{ metaKey: true }, { ctrlKey: true }, { metaKey: true }]) {
      await openPalette(false);
      // Lowercase, as a real Ctrl+K sends: cmdk's vim bindings used to swallow exactly this one.
      pressKey("k", mod, input());
      await closed();
    }
  });
});

describe("axe and leaks", () => {
  test.each(["light", "dark"])("axe: the palette in the %s theme (results, a scope, no results, a trace row)", async (theme) => {
    window.localStorage.setItem(THEME_STORAGE_KEY, theme);
    render(<KeyboardHarness />);
    const dialog = await openPalette();
    await waitFor(() => expect(document.documentElement.classList.contains("dark")).toBe(theme === "dark"));
    await expectNoAxeViolations(document.body);
    fireEvent.click(within(dialog).getByRole("radio", { name: "Agents" }));
    await expectNoAxeViolations(document.body);
    type("zzzzqq");
    await expectNoAxeViolations(document.body);
    type(RUN_ID);
    await expectNoAxeViolations(document.body);
  }, AXE_TIMEOUT_MS);

  test("axe: the palette with a failed list and while loading", async () => {
    data.tools.mockRejectedValue(new Error("HTTP 500"));
    data.mcp.mockReturnValue(new Promise(() => {}));
    render(<KeyboardHarness />);
    const dialog = await openPalette(false);
    await within(dialog).findByTestId("command-palette-error-tools");
    await expectNoAxeViolations(document.body);
  }, AXE_TIMEOUT_MS);

  test("the DOM shows only names, models and URLs: no prompts, descriptions, header names or ids in text", async () => {
    render(<KeyboardHarness />);
    const dialog = await openPalette();
    const text = dialog.textContent ?? "";
    for (const hidden of [SECRET, "X-Api-Key", WEATHER_ID, BILLING_ID, "srv-1", "wf-1", "streamable_http"]) {
      expect(text, hidden).not.toContain(hidden);
    }
    expect(dialog.innerHTML).not.toContain(SECRET);
    expect(dialog.innerHTML).not.toContain("X-Api-Key");
    expect(text).toContain("openai:gpt-test");
    expect(text).toContain("https://mcp.example.test");
  });

  test("entity ids appear only in the row's data attribute (the opaque test hook), nowhere in attributes like href or title", async () => {
    render(<KeyboardHarness />);
    const dialog = await openPalette();
    const holders = Array.from(dialog.querySelectorAll("*")).filter((el) =>
      Array.from(el.attributes).some((attr) => attr.value.includes(WEATHER_ID) && attr.name !== "data-palette-entity" && attr.name !== "data-value"),
    );
    expect(holders.map((el) => el.outerHTML.slice(0, 80))).toEqual([]);
    expect(dialog.querySelector("a[href]")).toBeNull();
  });
});

describe("g sequences", () => {
  const withKeys = allNavItems.filter((item) => item.gKey);
  const sleep = (ms: number) => act(() => void vi.advanceTimersByTime(ms));

  test("every nav item with a g-key opens its href from a fresh page, one push each", () => {
    expect(withKeys.length).toBeGreaterThan(0);
    render(<KeyboardHarness />);
    for (const item of withKeys) {
      nav.push.mockClear();
      pressKeys(G_PREFIX, item.gKey as string);
      expect(nav.push.mock.calls, item.id).toEqual([[item.href]]);
    }
  });

  test("the window is 1 s inclusive: the letter at exactly 1000 ms works, at 1001 ms it does not", () => {
    vi.useFakeTimers({ toFake: ["Date"] });
    render(<KeyboardHarness />);
    expect(SEQUENCE_TIMEOUT_MS).toBe(1000);
    pressKey("g");
    sleep(1000);
    pressKey("t");
    expect(nav.push.mock.calls).toEqual([[routes.traces()]]);
    nav.push.mockClear();
    pressKey("g");
    sleep(1001);
    pressKey("t");
    expect(nav.push).not.toHaveBeenCalled();
  });

  test("an expired g followed by a fresh g then a letter still navigates", () => {
    vi.useFakeTimers({ toFake: ["Date"] });
    render(<KeyboardHarness />);
    pressKey("g");
    sleep(2000);
    pressKeys("g", "l");
    expect(nav.push.mock.calls).toEqual([[routes.logs()]]);
  });

  test.each([["Escape"], ["Tab"], ["x"], ["ArrowDown"]])("%s between g and the letter cancels the sequence", (middle) => {
    render(<KeyboardHarness />);
    pressKeys("g", middle, "t");
    expect(nav.push).not.toHaveBeenCalled();
    pressKeys("g", "t");
    expect(nav.push.mock.calls).toEqual([[routes.traces()]]);
  });

  test("a sequence runs once: the letter alone afterwards does nothing, and two sequences in a row both run", () => {
    render(<KeyboardHarness />);
    pressKeys("g", "t", "t", "a");
    expect(nav.push.mock.calls).toEqual([[routes.traces()]]);
    pressKeys("g", "a", "g", "l");
    expect(nav.push.mock.calls.map(([href]) => href)).toEqual([routes.traces(), routes.agents(), routes.logs()]);
  });

  test("upper case G or a letter that no item owns navigates nowhere", () => {
    render(<KeyboardHarness />);
    pressKeys("G", "t");
    pressKeys("g", "z");
    pressKeys("g", "?");
    expect(nav.push).not.toHaveBeenCalled();
  });

  test("a g typed in a field is dropped, so the letter typed outside afterwards does not navigate", () => {
    render(<KeyboardHarness><input aria-label="Name" /></KeyboardHarness>);
    const field = screen.getByRole("textbox", { name: "Name" });
    pressKey("g", {}, field);
    pressKey("t");
    expect(nav.push).not.toHaveBeenCalled();
  });

  test("a g pressed outside, then a click into a field and the letter typed there, does not navigate", () => {
    render(<KeyboardHarness><input aria-label="Name" /></KeyboardHarness>);
    pressKey("g");
    pressKey("t", {}, screen.getByRole("textbox", { name: "Name" }));
    pressKey("t");
    expect(nav.push).not.toHaveBeenCalled();
  });
});

describe("the ? dialog matches the registry", () => {
  test("it lists exactly the registry's rows, every nav g-key with its letters", async () => {
    render(<KeyboardHarness />);
    pressKey("?", { shiftKey: true });
    const dialog = await screen.findByRole("dialog", { name: "Keyboard shortcuts" });
    const expected = shortcutSections().flatMap((section) => section.rows.map((row) => row.id));
    const shown = Array.from(dialog.querySelectorAll("[data-shortcut-row]")).map((row) => row.getAttribute("data-shortcut-row"));
    expect(shown).toEqual(expected);
    for (const item of allNavItems.filter((candidate) => candidate.gKey)) {
      const row = dialog.querySelector(`[data-shortcut-row="nav:${item.id}"]`) as HTMLElement;
      expect(row, item.id).not.toBeNull();
      expect(within(row).getByText(item.label)).toBeInTheDocument();
      expect(Array.from(row.querySelectorAll("kbd")).map((kbd) => kbd.textContent)).toEqual(["G", (item.gKey as string).toUpperCase()]);
    }
    const goTo = within(dialog).getByRole("heading", { name: "Go to" });
    expect(goTo).toBeInTheDocument();
    expect(dialog.querySelectorAll('[data-shortcut-row^="nav:"]')).toHaveLength(allNavItems.filter((item) => item.gKey).length);
  });

  test("items without a g-key (transitional pages) are not listed", async () => {
    render(<KeyboardHarness />);
    pressKey("?", { shiftKey: true });
    const dialog = await screen.findByRole("dialog", { name: "Keyboard shortcuts" });
    for (const item of allNavItems.filter((candidate) => !candidate.gKey)) {
      expect(dialog.querySelector(`[data-shortcut-row="nav:${item.id}"]`), item.id).toBeNull();
    }
  });

  test("the palette's Keyboard shortcuts command opens the same dialog; ? closes nothing while open", async () => {
    render(<KeyboardHarness />);
    const dialog = await openPalette();
    fireEvent.click(dialog.querySelector('[data-palette-command="shortcuts"]') as HTMLElement);
    const help = await screen.findByRole("dialog", { name: "Keyboard shortcuts" });
    expect(help.querySelectorAll("[data-shortcut-row]").length).toBe(shortcutSections().flatMap((s) => s.rows).length);
    pressKey("?", { shiftKey: true }, help);
    expect(screen.getAllByRole("dialog")).toHaveLength(1);
  });
});

describe("c: precedence (registered actions only)", () => {
  const Registers = ({ action }: { action: (() => void) | null }) => {
    useCreateAction(action);
    return null;
  };
  // The retired markup fallback's attribute: a button carrying it must never be clicked by `c`.
  const declared = { "data-shortcut-create": "" };

  test("the latest registration wins; a null registration registers nothing", () => {
    const first = vi.fn();
    const second = vi.fn();
    const { rerender } = render(<KeyboardHarness><Registers action={first} /><Registers action={null} /></KeyboardHarness>);
    pressKey("c");
    expect(first).toHaveBeenCalledTimes(1);
    rerender(<KeyboardHarness><Registers action={first} /><Registers action={second} /></KeyboardHarness>);
    pressKey("c");
    expect([first.mock.calls.length, second.mock.calls.length]).toEqual([1, 1]);
  });

  test("a registered action runs; a button with the old data-shortcut-create attribute is not clicked", () => {
    const action = vi.fn();
    const click = vi.fn();
    render(<KeyboardHarness><Registers action={action} /><button type="button" onClick={click} {...declared}>New</button></KeyboardHarness>);
    pressKey("c");
    expect(action).toHaveBeenCalledTimes(1);
    expect(click).not.toHaveBeenCalled();
  });

  test("with no registered action, c clicks no button in <main> (no markup fallback)", () => {
    const clicks: string[] = [];
    const btn = (name: string) => (
      <button type="button" onClick={() => clicks.push(name)} {...declared}>{name}</button>
    );
    render(<KeyboardHarness>{btn("one")}{btn("two")}</KeyboardHarness>);
    pressKey("c");
    expect(clicks).toEqual([]);
  });

  test("after the registering component unmounts, c does nothing again", () => {
    const action = vi.fn();
    const { rerender } = render(<KeyboardHarness><Registers action={action} /></KeyboardHarness>);
    rerender(<KeyboardHarness />);
    pressKey("c");
    expect(action).not.toHaveBeenCalled();
  });

  test("c is ignored in a field, with a modifier, and while a dialog is open", async () => {
    const action = vi.fn();
    render(<KeyboardHarness><Registers action={action} /><input aria-label="Name" /></KeyboardHarness>);
    pressKey("c", {}, screen.getByRole("textbox", { name: "Name" }));
    for (const mod of [{ ctrlKey: true }, { metaKey: true }, { altKey: true }]) pressKey("c", mod);
    expect(action).not.toHaveBeenCalled();
    pressKey("?", { shiftKey: true });
    await screen.findByRole("dialog", { name: "Keyboard shortcuts" });
    pressKey("c");
    expect(action).not.toHaveBeenCalled();
  });

  test("g then c does not run the create action", () => {
    const action = vi.fn();
    render(<KeyboardHarness><Registers action={action} /></KeyboardHarness>);
    pressKeys("g", "c");
    expect(action).not.toHaveBeenCalled();
    expect(nav.push).not.toHaveBeenCalled();
  });
});

describe("keys that must be ignored", () => {
  const create = vi.fn();
  const Page = () => {
    useCreateAction(create);
    return (
      <>
        <div contentEditable="true" suppressContentEditableWarning data-testid="ce">x</div>
        <div ref={(el) => el?.setAttribute("contenteditable", "")} data-testid="ce-empty">x</div>
        <div role="textbox" tabIndex={0} data-testid="role-textbox" />
        <textarea aria-label="Notes" />
        <select aria-label="Pick"><option>a</option></select>
        <input type="search" aria-label="Find" />
      </>
    );
  };
  beforeEach(() => create.mockClear());

  test.each([["ce"], ["ce-empty"], ["role-textbox"]])("%s: ?, c and g sequences do nothing", async (id) => {
    render(<KeyboardHarness><Page /></KeyboardHarness>);
    const target = screen.getByTestId(id);
    pressKey("?", { shiftKey: true }, target);
    pressKey("c", {}, target);
    pressKey("g", {}, target);
    pressKey("t", {}, target);
    expect(create).not.toHaveBeenCalled();
    expect(nav.push).not.toHaveBeenCalled();
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  test.each([["Notes", "textbox"], ["Pick", "combobox"], ["Find", "searchbox"]])("the %s %s ignores single keys", (name, role) => {
    render(<KeyboardHarness><Page /></KeyboardHarness>);
    const target = screen.getByRole(role, { name });
    pressKey("c", {}, target);
    pressKey("g", {}, target);
    pressKey("t", {}, target);
    expect(create).not.toHaveBeenCalled();
    expect(nav.push).not.toHaveBeenCalled();
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  test("IME composition: no key is treated as a shortcut, and a composing g does not arm a sequence", () => {
    render(<KeyboardHarness><Page /></KeyboardHarness>);
    for (const k of ["c", "?", "g", "t"]) pressKey(k, { isComposing: true });
    expect(create).not.toHaveBeenCalled();
    expect(nav.push).not.toHaveBeenCalled();
    expect(screen.queryByRole("dialog")).toBeNull();
    pressKey("g", { isComposing: true });
    pressKey("t");
    expect(nav.push).not.toHaveBeenCalled();
  });

  test("Ctrl, Meta and Alt make c, ? and g sequences inert", () => {
    render(<KeyboardHarness><Page /></KeyboardHarness>);
    for (const mod of [{ ctrlKey: true }, { metaKey: true }, { altKey: true }]) {
      pressKey("c", mod);
      pressKey("?", { ...mod, shiftKey: true });
      pressKey("g", mod);
      pressKey("t");
      pressKey("g");
      pressKey("t", mod);
    }
    expect(create).not.toHaveBeenCalled();
    expect(nav.push).not.toHaveBeenCalled();
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  test("a lone modifier press in the middle of g and a letter does not break the sequence", () => {
    render(<KeyboardHarness />);
    pressKey("g");
    pressKey("Shift");
    pressKey("t");
    expect(nav.push.mock.calls).toEqual([[routes.traces()]]);
  });

  test("a key whose default was already prevented is skipped", () => {
    render(<KeyboardHarness><Page /></KeyboardHarness>);
    const handler = (event: KeyboardEvent) => event.preventDefault();
    document.body.addEventListener("keydown", handler);
    pressKey("c");
    document.body.removeEventListener("keydown", handler);
    expect(create).not.toHaveBeenCalled();
  });
});

describe("no conflicts with / and [", () => {
  test("neither key is bound by the global bindings or used as a g-key; every g-key is unique", () => {
    const bindings = globalBindings();
    const allKeys = bindings.flatMap((binding) => binding.keys);
    expect(allKeys).not.toContain("/");
    expect(allKeys).not.toContain("[");
    const gKeys = allNavItems.map((item) => item.gKey).filter(Boolean) as string[];
    expect(new Set(gKeys).size).toBe(gKeys.length);
    expect(gKeys.filter((k) => ["/", "[", "?", G_PREFIX].includes(k))).toEqual([]);
    const sequences = bindings.map((binding) => binding.keys.join(" "));
    expect(new Set(sequences).size).toBe(sequences.length);
  });

  test("/ and [ never navigate or open help; [ still collapses the sidebar once", async () => {
    render(<KeyboardHarness />);
    pressKey("g");
    pressKey("/");
    pressKey("t");
    expect(nav.push).not.toHaveBeenCalled();
    pressKey("[");
    await waitFor(() => expect(screen.getByTestId("sidebar")).toHaveAttribute("data-collapsed", "true"));
    pressKey("[");
    await waitFor(() => expect(screen.getByTestId("sidebar")).toHaveAttribute("data-collapsed", "false"));
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(nav.push).not.toHaveBeenCalled();
  });

  test("the help rows list / and [ once each", async () => {
    const rows = shortcutSections().flatMap((section) => section.rows);
    const flat = rows.flatMap((row) => row.keys.flat());
    expect(flat.filter((k) => k === "/")).toHaveLength(1);
    expect(flat.filter((k) => k === "[")).toHaveLength(1);
  });
});

describe("Settings: clearing and browser data", () => {
  const LAYOUT = [SIDEBAR_STORAGE_KEY, SIDE_PANEL_STORAGE_KEY, `${AGENT_LAYOUT_PREFIX}a1`, LAST_AGENT_KEY];
  const PRESETS = [`${SAVED_CONTEXT_PREFIX}c1:a1`, `${SAVED_CONTEXT_PREFIX}c1:a2`, `${SAVED_CONTEXT_PREFIX}c2:a1`];
  const LOOKALIKES = ["eap.requestContext.v2:x", "eap.requestContext.v1", "eap.requestContext.v1x:y", "studio.agent-layout.v2.a1", "studio.lastAgentX", "other"];
  const store = window.localStorage;
  const stored = () => Object.keys({ ...store }).sort();
  const seed = (keys: string[], value = "{}") => keys.forEach((k) => store.setItem(k, value));
  const renderPage = () => render(<FrameHarness><SettingsPage /></FrameHarness>);
  const status = () => screen.getByTestId("settings-clear-status");
  async function confirm(testId: string, button: "Clear" | "Cancel") {
    fireEvent.click(screen.getByTestId(testId));
    const dialog = await screen.findByRole("dialog");
    fireEvent.click(within(dialog).getByRole("button", { name: button }));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  }

  test("clearing layout removes exactly the listed keys, never the theme, presets or look-alikes", async () => {
    seed(LAYOUT);
    seed(PRESETS);
    seed(LOOKALIKES);
    store.setItem(THEME_STORAGE_KEY, "dark");
    const remove = vi.spyOn(Storage.prototype, "removeItem");
    renderPage();
    await waitFor(() => expect(screen.getByTestId("browser-data-table")).toBeInTheDocument());
    await confirm("settings-clear-layout", "Clear");
    expect(remove.mock.calls.map(([k]) => k).sort()).toEqual([...LAYOUT].sort());
    expect(stored()).toEqual([...PRESETS, ...LOOKALIKES, THEME_STORAGE_KEY].sort());
    expect(store.getItem(THEME_STORAGE_KEY)).toBe("dark");
    expect(status()).toHaveTextContent("Layout preferences cleared (4 items).");
    remove.mockRestore();
  });

  test("clearing presets removes only eap.requestContext.v1:* keys", async () => {
    seed(LAYOUT);
    seed(PRESETS);
    seed(LOOKALIKES);
    store.setItem(THEME_STORAGE_KEY, "light");
    const remove = vi.spyOn(Storage.prototype, "removeItem");
    renderPage();
    await confirm("settings-clear-presets", "Clear");
    expect(remove.mock.calls.map(([k]) => k).sort()).toEqual([...PRESETS].sort());
    expect(stored()).toEqual([...LAYOUT, ...LOOKALIKES, THEME_STORAGE_KEY].sort());
    expect(status()).toHaveTextContent("Request-context presets cleared (3 items).");
    remove.mockRestore();
  });

  test("the status counts: one item is singular, none says so, and the toast repeats the result", async () => {
    seed([LAST_AGENT_KEY]);
    renderPage();
    await confirm("settings-clear-layout", "Clear");
    expect(status()).toHaveTextContent("Layout preferences cleared (1 item).");
    // Sonner keeps toasts in module state, so earlier tests may have left the same text behind.
    expect((await screen.findAllByText("Layout preferences cleared")).length).toBeGreaterThan(0);
    await confirm("settings-clear-layout", "Clear");
    expect(status()).toHaveTextContent("There were no layout preferences to clear.");
    await confirm("settings-clear-presets", "Clear");
    expect(status()).toHaveTextContent("There were no request-context presets to clear.");
  });

  test("the confirm describes what goes, and Cancel (button or Escape) changes nothing", async () => {
    seed(LAYOUT);
    seed(PRESETS);
    store.setItem(THEME_STORAGE_KEY, "dark");
    const before = stored();
    const remove = vi.spyOn(Storage.prototype, "removeItem");
    renderPage();
    fireEvent.click(screen.getByTestId("settings-clear-presets"));
    const dialog = await screen.findByRole("dialog", { name: "Clear request-context presets?" });
    expect(dialog).toHaveTextContent("cannot be undone");
    expect(status()).toBeEmptyDOMElement();
    fireEvent.click(within(dialog).getByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    fireEvent.click(screen.getByTestId("settings-clear-layout"));
    fireEvent.keyDown(await screen.findByRole("dialog"), { key: "Escape" });
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(remove).not.toHaveBeenCalled();
    expect(stored()).toEqual(before);
    expect(status()).toBeEmptyDOMElement();
    remove.mockRestore();
  });
});

describe("Settings: storage failures and the data table", () => {
  const store = window.localStorage;
  const renderPage = () => render(<FrameHarness><SettingsPage /></FrameHarness>);
  const row = (id: string) => screen.getByTestId("browser-data-table").querySelector(`[data-browser-data="${id}"]`) as HTMLElement;

  test("the table lists every registry entry, in order, with no key names or values", async () => {
    store.setItem(`${SAVED_CONTEXT_PREFIX}c1:${WEATHER_ID}`, JSON.stringify({ token: SECRET }));
    store.setItem(`${AGENT_LAYOUT_PREFIX}${BILLING_ID}`, SECRET);
    renderPage();
    await waitFor(() => expect(row("request-context")).toHaveTextContent("1 saved"));
    const ids = Array.from(screen.getByTestId("browser-data-table").querySelectorAll("[data-browser-data]")).map((r) => r.getAttribute("data-browser-data"));
    expect(ids).toEqual(BROWSER_DATA.map((entry) => entry.id));
    const text = document.body.textContent ?? "";
    for (const hidden of [SECRET, WEATHER_ID, BILLING_ID, SAVED_CONTEXT_PREFIX, AGENT_LAYOUT_PREFIX]) expect(text, hidden).not.toContain(hidden);
  });

  test("each row reports its stored keys accurately: Not set, Saved, or N saved", async () => {
    store.setItem(SIDEBAR_STORAGE_KEY, "collapsed");
    store.setItem(THEME_STORAGE_KEY, "dark");
    for (const k of ["a", "b", "c"]) store.setItem(`${AGENT_LAYOUT_PREFIX}${k}`, "{}");
    store.setItem(`${SAVED_CONTEXT_PREFIX}x:y`, "{}");
    store.setItem("eap.requestContext.v2:x", "{}");
    renderPage();
    await waitFor(() => expect(row("agent-layout")).toHaveTextContent("3 saved"));
    expect(row("theme")).toHaveTextContent("Saved");
    expect(row("sidebar")).toHaveTextContent("Saved");
    expect(row("side-panel")).toHaveTextContent("Not set");
    expect(row("last-agent")).toHaveTextContent("Not set");
    expect(row("request-context")).toHaveTextContent("1 saved");
    expect(row("request-context")).not.toHaveTextContent("2 saved");
  });

  test("the table follows a clear: counts drop to Not set, other rows keep theirs", async () => {
    store.setItem(THEME_STORAGE_KEY, "dark");
    store.setItem(`${AGENT_LAYOUT_PREFIX}a`, "{}");
    store.setItem(`${SAVED_CONTEXT_PREFIX}x:y`, "{}");
    renderPage();
    await waitFor(() => expect(row("agent-layout")).toHaveTextContent("1 saved"));
    fireEvent.click(screen.getByTestId("settings-clear-layout"));
    fireEvent.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "Clear" }));
    await waitFor(() => expect(row("agent-layout")).toHaveTextContent("Not set"));
    expect(row("request-context")).toHaveTextContent("1 saved");
    expect(row("theme")).toHaveTextContent("Saved");
  });

  test("removeItem throwing: nothing is claimed cleared, the status explains, the page survives", async () => {
    store.setItem(LAST_AGENT_KEY, "a1");
    renderPage();
    const remove = vi.spyOn(Storage.prototype, "removeItem").mockImplementation(() => {
      throw new DOMException("denied", "SecurityError");
    });
    fireEvent.click(screen.getByTestId("settings-clear-layout"));
    fireEvent.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "Clear" }));
    expect(await screen.findByText("Browser storage is not available, so nothing was cleared.")).toBeInTheDocument();
    expect(screen.getByRole("heading", { level: 1, name: "Settings" })).toBeInTheDocument();
    remove.mockRestore();
    expect(store.getItem(LAST_AGENT_KEY)).toBe("a1");
  });

  test("localStorage itself throwing: the page renders, the table shows Not set, Clear says unavailable", async () => {
    const getter = vi.spyOn(window, "localStorage", "get").mockImplementation(() => {
      throw new DOMException("blocked", "SecurityError");
    });
    renderPage();
    expect(screen.getByRole("heading", { level: 2, name: "Data in this browser" })).toBeInTheDocument();
    await waitFor(() => expect(row("agent-layout")).toHaveTextContent("Not set"));
    fireEvent.click(screen.getByTestId("settings-clear-presets"));
    fireEvent.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "Clear" }));
    expect(await screen.findByTestId("settings-clear-status")).toHaveTextContent("Browser storage is not available");
    getter.mockRestore();
  });

  test("the theme control never touches the other keys", async () => {
    store.setItem(LAST_AGENT_KEY, "a1");
    renderPage();
    fireEvent.click(within(screen.getByTestId("settings-theme")).getByRole("radio", { name: "Dark" }));
    await waitFor(() => expect(store.getItem(THEME_STORAGE_KEY)).toBe("dark"));
    expect(Object.keys({ ...store }).sort()).toEqual([LAST_AGENT_KEY, THEME_STORAGE_KEY].sort());
  });

  test("the Settings shortcut table matches the help dialog's rows", () => {
    renderPage();
    const shown = Array.from(screen.getByTestId("settings-shortcuts").querySelectorAll("[data-shortcut-row]")).map((r) => r.getAttribute("data-shortcut-row"));
    expect(shown).toEqual(shortcutSections().flatMap((section) => section.rows.map((r) => r.id)));
  });
});
