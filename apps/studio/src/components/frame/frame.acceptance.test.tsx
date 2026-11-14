/**
 * SL.4 acceptance: every page inside the real frame, and the frame's behaviour end to end.
 * Complements the unit tests next to each frame component (not duplicated here).
 */
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import React from "react";
import fs from "node:fs";
import path from "node:path";
import { expectNoAxeViolations, AXE_TIMEOUT_MS, PAGE_RULES } from "@/test/a11y";
import { routes } from "@/lib/routes";

const nav = vi.hoisted(() => ({ path: "/agents", params: {} as Record<string, string> }));

vi.mock("next/navigation", () => ({
  usePathname: () => nav.path,
  useParams: () => nav.params,
  useRouter: () => ({ push: vi.fn(), replace: vi.fn(), back: vi.fn(), prefetch: vi.fn() }),
  useSearchParams: () => new URLSearchParams(),
}));

import { FrameHarness, mockMatchMedia, type MatchMediaControl } from "./frameTesting";
import { PageHeader } from "./PageHeader";
import { SidePanel, useSidePanel } from "./SidePanel";
import { SIDEBAR_STORAGE_KEY, SIDE_PANEL_STORAGE_KEY } from "./frameStorage";
import { navSections, navItems } from "@/lib/nav";
import { breakdownFixture, summaryFixture, timeseriesFixture } from "@/components/metrics/metricsFixtures";

let media: MatchMediaControl | undefined;

const AGENT = {
  id: "a1",
  name: "Support bot",
  latest_version: 1,
  draft_spec: { spec_version: 1, name: "Support bot", instructions: "Help.", model: { provider: "test", name: "echo" }, tools: [] },
};

/** A generic API: lists are empty, single agents exist, everything else is an empty page. */
function respond(url: URL): unknown {
  const p = url.pathname;
  if (/^\/api\/v1\/runs\/[^/]+\/events$/.test(p)) return [];
  const run = /^\/api\/v1\/runs\/([^/]+)$/.exec(p);
  if (run) return { run_id: run[1], kind: "agent", agent_id: "a1", version: 1, status: "completed", display_status: "completed", input: "hi", output: "ok", error: null, heartbeat_at: null, created_at: "2026-11-14T10:00:00Z", updated_at: "2026-11-14T10:00:01Z", trace_id: null, conversation_id: null, turn: null };
  if (/^\/api\/v1\/agents\/[^/]+$/.test(p)) return AGENT;
  if (p === "/api/v1/agents") return [AGENT];
  if (p === "/api/v1/models") return { items: [] };
  if (p === "/api/v1/metrics/summary") return summaryFixture();
  if (p === "/api/v1/metrics/timeseries") return timeseriesFixture();
  if (p === "/api/v1/metrics/breakdown") return breakdownFixture(url.searchParams.get("by") === "agent" ? "agent" : "model", []);
  if (p === "/api/v1/processors") return { items: [], moderation_categories: [], limits: { max_processors_per_phase: 4, max_regex_patterns: 10, max_regex_pattern_length: 200, max_test_sample_bytes: 4096 } };
  if (p === "/api/v1/mcp-servers") return [];
  if (p === "/api/v1/knowledge-bases") return { items: [] };
  if (p === "/api/v1/knowledge-bases/kb1") return { id: "kb1", tenant_id: "t1", name: "Product docs", description: null, embedding_model: "test:hash", dimensions: 1536, chunking: {}, active_generation: 1, last_generation: 1, pending_generation: null, pending_embedding_model: null, pending_dimensions: null, status: "ready", error: null, created_at: "2026-11-14T10:00:00Z", updated_at: "2026-11-14T10:00:00Z", document_count: 0, chunk_count: 0, ingest_tokens: 0, ingest_cost_usd: "0.0000000000", ingest_requests: 0 };
  if (p === "/api/v1/knowledge-bases/kb1/documents") return { items: [], next_after: null };
  if (p === "/api/v1/workflows" || p === "/api/v1/tools") return p.endsWith("tools") ? { items: [] } : [];
  if (/\/(versions|tools|processors|servers)$/.test(p)) return [];
  return { items: [], next_before: null, total: 0 };
}

function stubApi() {
  vi.stubEnv("NEXT_PUBLIC_EAP_DEV_TOKEN", "test-token");
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: unknown) => {
      const url = new URL(String(typeof input === "string" ? input : (input as Request).url), "http://localhost");
      return new Response(JSON.stringify(respond(url)), { status: 200, headers: { "content-type": "application/json" } });
    }),
  );
}

function setViewport(width: number) {
  Object.defineProperty(window, "innerWidth", { configurable: true, writable: true, value: width });
  window.dispatchEvent(new Event("resize"));
}

beforeEach(() => {
  document.documentElement.lang = "en";
  document.documentElement.removeAttribute("data-theme");
  document.documentElement.classList.remove("dark");
  window.localStorage.clear();
  nav.path = routes.agents();
  nav.params = {};
  setViewport(1440);
  stubApi();
});

afterEach(() => {
  cleanup();
  media?.restore();
  media = undefined;
  vi.unstubAllGlobals();
  vi.unstubAllEnvs();
});

const sidebar = () => screen.getByTestId("sidebar");
const press = (key: string, init: KeyboardEventInit = {}, target: Element | Document = document.body) =>
  fireEvent.keyDown(target, { key, ...init });

interface PageCase {
  name: string;
  path: string;
  params?: Record<string, string>;
  load: () => Promise<{ default: React.ComponentType }>;
  /** The visible h1 (last breadcrumb); null for the two run views that keep their own header until a later task. */
  h1: string | null;
  active: string;
}

/** An agent tab page inside `app/agents/[id]/layout.tsx`, as Next renders it. */
async function inAgentLayout(tab: Promise<{ default: React.ComponentType }>): Promise<{ default: React.ComponentType }> {
  const [{ default: Layout }, { default: Tab }] = await Promise.all([import("@/app/agents/[id]/layout"), tab]);
  return {
    default: function AgentTabPage() {
      return (
        <Layout>
          <Tab />
        </Layout>
      );
    },
  };
}

const PAGES: PageCase[] = [
  { name: "agents", path: "/agents", load: () => import("@/app/agents/page"), h1: "Agents", active: "agents" },
  { name: "agents/new", path: "/agents/new", load: () => import("@/app/agents/new/page"), h1: "New Agent", active: "agents" },
  // SL.6: `/agents/:id` and `/playground` redirect; the agent page's tabs render inside its layout.
  { name: "agent chat", path: "/agents/a1/chat", params: { id: "a1" }, load: () => inAgentLayout(import("@/app/agents/[id]/chat/page")), h1: "Support bot", active: "agents" },
  { name: "agent traces", path: "/agents/a1/traces", params: { id: "a1" }, load: () => inAgentLayout(import("@/app/agents/[id]/traces/page")), h1: "Support bot", active: "agents" },
  { name: "agent editor", path: "/agents/a1/editor", params: { id: "a1" }, load: () => inAgentLayout(import("@/app/agents/[id]/editor/page")), h1: "Support bot", active: "agents" },
  { name: "workflows", path: "/workflows", load: () => import("@/app/workflows/page"), h1: "Workflows", active: "workflows" },
  { name: "workflows/[id]", path: "/workflows/w1", params: { id: "w1" }, load: () => import("@/app/workflows/[id]/page"), h1: "Workflow", active: "workflows" },
  { name: "workflow run", path: "/workflows/w1/runs/r1", params: { id: "w1", runId: "r1" }, load: () => import("@/app/workflows/[id]/runs/[runId]/page"), h1: null, active: "workflows" },
  { name: "tools", path: "/tools", load: () => import("@/app/tools/page"), h1: "Tools", active: "tools" },
  { name: "mcp", path: "/mcp", load: () => import("@/app/mcp/page"), h1: "MCP Servers", active: "mcp" },
  { name: "processors", path: "/processors", load: () => import("@/app/processors/page"), h1: "Processors", active: "processors" },
  { name: "knowledge", path: "/knowledge", load: () => import("@/app/knowledge/page"), h1: "Knowledge", active: "knowledge" },
  { name: "knowledge base", path: "/knowledge/kb1", params: { id: "kb1" }, load: () => import("@/app/knowledge/[id]/page"), h1: "Product docs", active: "knowledge" },
  // SL.10: `/request-context` redirects to the chat with its request context popover open (`context=open`).
  {
    name: "request-context",
    path: "/agents/a1/chat",
    params: { id: "a1" },
    load: () => {
      window.history.replaceState(null, "", routes.agentChat("a1", { context: "open" }));
      return inAgentLayout(import("@/app/agents/[id]/chat/page"));
    },
    h1: "Support bot",
    active: "agents",
  },
  { name: "metrics", path: "/metrics", load: () => import("@/app/metrics/page"), h1: "Metrics", active: "metrics" },
  { name: "traces", path: "/traces", load: () => import("@/app/traces/page"), h1: "Traces", active: "traces" },
  // SL.12: `/traces/:runId` and `/runs/:id` are redirects; a run's trace and events are the trace Sheet
  // over the list, checked inside this frame (light and dark, axe) in components/traces/TraceSheet.frame.test.tsx.
  { name: "logs", path: "/logs", load: () => import("@/app/logs/page"), h1: "Logs", active: "logs" },
  { name: "settings", path: "/settings", load: () => import("@/app/settings/page"), h1: "Settings", active: "settings" },
];

async function renderPage(page: PageCase, wide = true) {
  nav.path = page.path;
  nav.params = page.params ?? {};
  media = mockMatchMedia(wide);
  const Page = (await page.load()).default;
  return render(<FrameHarness><Page /></FrameHarness>);
}

describe.each(["light", "dark"] as const)("every page inside the frame (%s theme)", (theme) => {
  beforeEach(() => {
    window.localStorage.setItem("studio.theme", theme);
    document.documentElement.classList.toggle("dark", theme === "dark");
  });

  afterEach(() => {
    window.localStorage.removeItem("studio.theme");
    document.documentElement.classList.remove("dark");
    document.documentElement.style.colorScheme = "";
  });

  test.each(PAGES)("$name: one main, the active nav item, an h1 equal to the last crumb, no axe violations", async (page) => {
    await renderPage(page);
    expect(document.documentElement.classList.contains("dark")).toBe(theme === "dark");
    const active = sidebar().querySelectorAll('[aria-current="page"]');
    expect(active).toHaveLength(1);
    expect(active[0].getAttribute("data-nav-id")).toBe(page.active);
    const main = screen.getByRole("main");
    if (page.h1) {
      // Entity pages show their title once the entity has loaded.
      await waitFor(() => expect(within(main).getAllByRole("heading", { level: 1 })[0]).toHaveTextContent(page.h1!));
      const h1s = within(main).getAllByRole("heading", { level: 1 });
      expect(h1s).toHaveLength(1);
      expect(h1s[0]).toHaveTextContent(page.h1);
      const header = within(main).getByTestId("page-header");
      const trail = header.querySelectorAll("nav[aria-label] li");
      expect(trail[trail.length - 1]?.textContent ?? h1s[0].textContent).toContain(page.h1);
      await waitFor(() => expect(document.title).toContain(page.h1!));
    }
    await act(async () => { await new Promise((resolve) => setTimeout(resolve, 20)); });
    expect(document.querySelectorAll("main, [role='main']")).toHaveLength(1);
    await expectNoAxeViolations(document, { rules: PAGE_RULES });
  }, AXE_TIMEOUT_MS);
});

function Body() {
  return (
    <>
      <h1>Body page</h1>
      <input aria-label="Field" />
      <textarea aria-label="Notes" />
      <div contentEditable suppressContentEditableWarning aria-label="Rich" role="textbox" />
    </>
  );
}

describe("sidebar", () => {
  beforeEach(() => { media = mockMatchMedia(true); });

  test("[ collapses and expands, and the choice is saved and restored on a fresh mount", () => {
    const first = render(<FrameHarness><Body /></FrameHarness>);
    expect(sidebar()).toHaveAttribute("data-collapsed", "false");
    press("[");
    expect(sidebar()).toHaveAttribute("data-collapsed", "true");
    expect(window.localStorage.getItem(SIDEBAR_STORAGE_KEY)).toBe("collapsed");
    first.unmount();
    render(<FrameHarness><Body /></FrameHarness>);
    expect(sidebar()).toHaveAttribute("data-collapsed", "true");
    press("[");
    expect(window.localStorage.getItem(SIDEBAR_STORAGE_KEY)).toBe("expanded");
  });

  test("[ is ignored in every kind of text field, with Ctrl, Alt or Meta, and while a dialog is open", () => {
    render(<FrameHarness><Body /></FrameHarness>);
    for (const label of ["Field", "Notes", "Rich"]) press("[", {}, screen.getByLabelText(label));
    for (const mod of [{ ctrlKey: true }, { altKey: true }, { metaKey: true }]) press("[", mod);
    expect(sidebar()).toHaveAttribute("data-collapsed", "false");
    const modal = document.createElement("div");
    modal.setAttribute("role", "dialog");
    document.body.appendChild(modal);
    press("[");
    expect(sidebar()).toHaveAttribute("data-collapsed", "false");
    modal.remove();
    press("[");
    expect(sidebar()).toHaveAttribute("data-collapsed", "true");
  });

  test("[ is ignored while the theme menu is open", async () => {
    render(<FrameHarness><Body /></FrameHarness>);
    fireEvent.keyDown(screen.getByTestId("theme-menu-trigger"), { key: "Enter" });
    await screen.findByRole("menu");
    press("[", {}, screen.getByRole("menu"));
    expect(sidebar()).toHaveAttribute("data-collapsed", "false");
  });

  test("groups render in registry order, with the footer last; empty groups are not rendered", () => {
    render(<FrameHarness />);
    const groups = Array.from(sidebar().querySelectorAll("[data-nav-group]")).map((el) => el.getAttribute("data-nav-group"));
    expect(groups).toEqual(navSections.map((section) => section.group.id));
    expect(groups).not.toContain("approvals");
    expect(groups).not.toContain("evaluation");
    for (const el of sidebar().querySelectorAll("[data-nav-group]")) expect(el.querySelectorAll("li").length).toBeGreaterThan(0);
    expect(groups[groups.length - 1]).toBe("settings");
    expect(within(screen.getByTestId("sidebar-footer")).getByRole("link", { name: "Settings" })).toBeInTheDocument();
  });

  test("links are in tab order, in registry order, and only the active one has aria-current", () => {
    nav.path = "/tools";
    render(<FrameHarness />);
    const hrefs = Array.from(sidebar().querySelectorAll("a")).map((a) => a.getAttribute("data-nav-id"));
    expect(hrefs).toEqual(navSections.flatMap((section) => section.items.map((item) => item.id)));
    const current = Array.from(sidebar().querySelectorAll("a[aria-current]"));
    expect(current.map((a) => a.getAttribute("data-nav-id"))).toEqual(["tools"]);
    for (const a of sidebar().querySelectorAll("a")) expect(a).not.toHaveAttribute("tabindex", "-1");
  });

  test("the rail keeps every item reachable by name, and shows its name as a tooltip on hover and focus", async () => {
    window.localStorage.setItem(SIDEBAR_STORAGE_KEY, "collapsed");
    render(<FrameHarness />);
    for (const item of navItems) {
      const link = within(sidebar()).getByRole("link", { name: item.label });
      expect(link).toHaveAttribute("href", item.href);
    }
    const logs = within(sidebar()).getByRole("link", { name: "Logs" });
    act(() => logs.focus());
    expect(await screen.findByRole("tooltip")).toHaveTextContent("Logs");
    act(() => logs.blur());
    fireEvent.pointerMove(within(sidebar()).getByRole("link", { name: "Metrics" }));
    act(() => within(sidebar()).getByRole("link", { name: "Metrics" }).focus());
    await waitFor(() => expect(screen.getByRole("tooltip")).toHaveTextContent("Metrics"));
  });
});

describe("mobile, below 1024 px", () => {
  beforeEach(() => {
    media = mockMatchMedia(false);
    setViewport(400);
  });
  const openButton = () => screen.getByRole("button", { name: "Open navigation" });
  const drawer = () => screen.getByTestId("mobile-nav-drawer");
  const openDrawer = async () => {
    openButton().focus();
    fireEvent.click(openButton());
    await screen.findByTestId("mobile-nav-drawer");
  };

  test("the menu button opens the drawer with every nav group; Escape closes it and focus returns", async () => {
    render(<FrameHarness><Body /></FrameHarness>);
    expect(screen.queryByTestId("mobile-nav-drawer")).toBeNull();
    await openDrawer();
    expect(within(drawer()).getByRole("link", { name: "Agents" })).toBeInTheDocument();
    expect(drawer()).toHaveAttribute("role", "dialog");
    fireEvent.keyDown(drawer(), { key: "Escape" });
    await waitFor(() => expect(screen.queryByTestId("mobile-nav-drawer")).toBeNull());
    await waitFor(() => expect(openButton()).toHaveFocus());
  });

  test("the close button and the backdrop close it", async () => {
    render(<FrameHarness><Body /></FrameHarness>);
    await openDrawer();
    fireEvent.click(screen.getByTestId("mobile-nav-close"));
    await waitFor(() => expect(screen.queryByTestId("mobile-nav-drawer")).toBeNull());
    await openDrawer();
    const backdrop = drawer().previousElementSibling as HTMLElement;
    expect(backdrop).not.toBeNull();
    fireEvent.pointerDown(backdrop);
    fireEvent.click(backdrop);
    await waitFor(() => expect(screen.queryByTestId("mobile-nav-drawer")).toBeNull());
  });

  test("following a link closes the drawer", async () => {
    render(<FrameHarness><Body /></FrameHarness>);
    await openDrawer();
    const link = within(drawer()).getByRole("link", { name: "Logs" });
    link.addEventListener("click", (event) => event.preventDefault());
    fireEvent.click(link);
    await waitFor(() => expect(screen.queryByTestId("mobile-nav-drawer")).toBeNull());
  });

  test("focus starts inside the drawer, and Tab and Shift+Tab stay inside it", async () => {
    render(<FrameHarness><Body /></FrameHarness>);
    openButton().focus();
    await openDrawer();
    await waitFor(() => expect(drawer().contains(document.activeElement)).toBe(true));
    const focusable = Array.from(drawer().querySelectorAll<HTMLElement>("a[href], button:not([disabled])"));
    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    expect(focusable.length).toBeGreaterThan(2);
    // Tab from the last element wraps to the first...
    act(() => last.focus());
    fireEvent.keyDown(last, { key: "Tab" });
    expect(document.activeElement).toBe(first);
    // ...and Shift+Tab from the first wraps to the last.
    fireEvent.keyDown(first, { key: "Tab", shiftKey: true });
    expect(document.activeElement).toBe(last);
    // Everything behind the modal is hidden from assistive tech.
    expect(screen.queryByRole("heading", { level: 1, name: "Body page" })).toBeNull();
  });

  test("[ does nothing: no rail, nothing saved", () => {
    render(<FrameHarness><Body /></FrameHarness>);
    press("[");
    expect(sidebar()).toHaveAttribute("data-collapsed", "false");
    expect(window.localStorage.getItem(SIDEBAR_STORAGE_KEY)).toBeNull();
  });

  test("no element in the frame is wider than a 400 px viewport", async () => {
    const wide: string[] = [];
    const check = (root: ParentNode) => {
      for (const el of Array.from(root.querySelectorAll<HTMLElement>("*"))) {
        const classes = (el.getAttribute("class") ?? "").split(/\s+/);
        for (const cls of classes) {
          // Only unprefixed utilities apply at 400 px (sm:, md:, lg: start at 640 px and up).
          const px = /^-?(?:min-)?w-\[(\d+(?:\.\d+)?)px\]$/.exec(cls)?.[1];
          const rem = /^(?:min-)?w-\[(\d+(?:\.\d+)?)rem\]$/.exec(cls)?.[1];
          const step = /^(?:min-)?w-(\d+)$/.exec(cls)?.[1];
          const width = px ? Number(px) : rem ? Number(rem) * 16 : step ? Number(step) * 4 : 0;
          if (width > 400) wide.push(`${el.tagName.toLowerCase()}.${cls}`);
        }
        const inline = el.style.width || el.style.minWidth;
        if (/^\d+(\.\d+)?px$/.test(inline) && parseFloat(inline) > 400) wide.push(`${el.tagName.toLowerCase()} style=${inline}`);
      }
    };
    render(<FrameHarness><Body /></FrameHarness>);
    check(document.body);
    await act(async () => { fireEvent.click(openButton()); });
    await screen.findByTestId("mobile-nav-drawer");
    check(document.body);
    expect(wide).toEqual([]);
    expect(drawer().className).toMatch(/max-w-\[85vw\]/);
  });
});

describe("theme menu", () => {
  beforeEach(() => { media = mockMatchMedia(true); });
  const pick = async (name: "System" | "Light" | "Dark") => {
    fireEvent.keyDown(screen.getByTestId("theme-menu-trigger"), { key: "Enter" });
    fireEvent.click(await screen.findByRole("menuitemradio", { name }));
    await waitFor(() => expect(screen.queryByRole("menu")).toBeNull());
  };

  test("switches Dark, Light and System, persists the choice, and restores it on a fresh mount", async () => {
    const first = render(<FrameHarness />);
    const trigger = () => screen.getByTestId("theme-menu-trigger");
    expect(trigger()).toHaveAccessibleName("Theme: System");
    await pick("Dark");
    expect(document.documentElement.classList.contains("dark")).toBe(true);
    expect(window.localStorage.getItem("studio.theme")).toBe("dark");
    expect(trigger()).toHaveAccessibleName("Theme: Dark");
    await pick("Light");
    expect(document.documentElement.classList.contains("dark")).toBe(false);
    expect(window.localStorage.getItem("studio.theme")).toBe("light");
    first.unmount();
    render(<FrameHarness />);
    expect(screen.getByTestId("theme-menu-trigger")).toHaveAccessibleName("Theme: Light");
    await pick("System");
    expect(screen.getByTestId("theme-menu-trigger")).toHaveAccessibleName("Theme: System");
    expect(window.localStorage.getItem("studio.theme")).not.toMatch(/dark|light/);
    expect(document.documentElement.classList.contains("dark")).toBe(false);
  });

  test("the open menu offers exactly System, Light and Dark, with the current one checked", async () => {
    window.localStorage.setItem("studio.theme", "dark");
    render(<FrameHarness />);
    fireEvent.keyDown(screen.getByTestId("theme-menu-trigger"), { key: "Enter" });
    const items = await screen.findAllByRole("menuitemradio");
    expect(items.map((item) => item.textContent)).toEqual(["System", "Light", "Dark"]);
    expect(items.map((item) => item.getAttribute("aria-checked"))).toEqual(["false", "false", "true"]);
  });

  test("the rail's theme trigger also switches the theme", async () => {
    window.localStorage.setItem(SIDEBAR_STORAGE_KEY, "collapsed");
    render(<FrameHarness />);
    await pick("Dark");
    expect(window.localStorage.getItem("studio.theme")).toBe("dark");
  });
});

describe("skip link and document title", () => {
  beforeEach(() => { media = mockMatchMedia(true); });

  test("the skip link is first in tab order and moves focus to main on every page layout", () => {
    render(<FrameHarness><Body /></FrameHarness>);
    const skip = screen.getByTestId("skip-link");
    expect(document.body.querySelector("a, button, input, [tabindex]:not([tabindex='-1'])")).toBe(skip);
    skip.focus();
    fireEvent.click(skip);
    expect(screen.getByTestId("main-card")).toHaveFocus();
    expect(screen.getByRole("main")).toHaveAttribute("id", "main");
  });

  test("document.title follows the header title and changes with it", () => {
    const { rerender } = render(<FrameHarness><PageHeader crumbs={[{ label: "Agents" }]} /></FrameHarness>);
    expect(document.title).toBe("Agents · AI Platform Studio");
    rerender(<FrameHarness><PageHeader crumbs={[{ label: "Agents", href: "/agents" }, { label: "Support bot" }]} /></FrameHarness>);
    expect(document.title).toBe("Support bot · AI Platform Studio");
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent("Support bot");
    rerender(<FrameHarness><PageHeader crumbs={[{ label: "Logs" }]} documentTitle="Run logs" /></FrameHarness>);
    expect(document.title).toBe("Run logs · AI Platform Studio");
  });
});

function Panel({ defaultOpen = true }: { defaultOpen?: boolean }) {
  const panel = useSidePanel();
  return (
    <>
      <button type="button" onClick={panel.toggle}>Config</button>
      <SidePanel title="Config" defaultOpen={defaultOpen}><p>Model</p></SidePanel>
    </>
  );
}

describe("side panel", () => {
  const handle = () => screen.getByRole("separator", { name: "Resize side panel" });
  const key = (name: string) => fireEvent.keyDown(handle(), { key: name });

  test("Home, End and the arrows stay within 360 px and half the window, and a remount restores the width", () => {
    media = mockMatchMedia(true);
    const first = render(<FrameHarness><Panel /></FrameHarness>);
    key("Home");
    expect(handle()).toHaveAttribute("aria-valuenow", "360");
    key("ArrowRight");
    expect(handle()).toHaveAttribute("aria-valuenow", "360");
    key("End");
    expect(handle()).toHaveAttribute("aria-valuenow", "720");
    key("ArrowLeft");
    expect(handle()).toHaveAttribute("aria-valuenow", "720");
    key("ArrowRight");
    expect(Number(handle().getAttribute("aria-valuenow"))).toBeLessThan(720);
    const saved = JSON.parse(window.localStorage.getItem(SIDE_PANEL_STORAGE_KEY)!).width;
    first.unmount();
    render(<FrameHarness><Panel /></FrameHarness>);
    expect(handle()).toHaveAttribute("aria-valuenow", String(saved));
  });

  test.each(["not json", '{"width":"wide"}', '{"width":-5}', '{"width":0}', "[400]", "null", '{"width":null}', '{"wid":500}'])(
    "stored data %s is ignored and the default width is used",
    (raw) => {
      media = mockMatchMedia(true);
      window.localStorage.setItem(SIDE_PANEL_STORAGE_KEY, raw);
      render(<FrameHarness><Panel /></FrameHarness>);
      expect(handle()).toHaveAttribute("aria-valuenow", "400");
    },
  );

  test("a stored width wider than half the window is clamped; one below 360 px is raised", () => {
    media = mockMatchMedia(true);
    window.localStorage.setItem(SIDE_PANEL_STORAGE_KEY, JSON.stringify({ width: 5000 }));
    const first = render(<FrameHarness><Panel /></FrameHarness>);
    expect(handle()).toHaveAttribute("aria-valuenow", "720");
    first.unmount();
    window.localStorage.setItem(SIDE_PANEL_STORAGE_KEY, JSON.stringify({ width: 100 }));
    render(<FrameHarness><Panel /></FrameHarness>);
    expect(handle()).toHaveAttribute("aria-valuenow", "360");
  });

  test("below 1024 px it is a Sheet: no column, no resize handle, nothing saved", async () => {
    media = mockMatchMedia(false);
    render(<FrameHarness><Panel defaultOpen={false} /></FrameHarness>);
    fireEvent.click(screen.getByRole("button", { name: "Config" }));
    const sheet = await screen.findByRole("dialog", { name: "Config" });
    expect(within(sheet).getByText("Model")).toBeInTheDocument();
    expect(screen.queryByRole("separator")).toBeNull();
    expect(screen.queryByRole("complementary", { name: "Config" })).toBeNull();
    expect(window.localStorage.getItem(SIDE_PANEL_STORAGE_KEY)).toBeNull();
  });
});

describe("no Mastra branding in the frame or the nav registry", () => {
  test("no source file under components/frame or lib/nav mentions Mastra", () => {
    const root = path.resolve(__dirname, "..", "..");
    const files = [
      ...fs.readdirSync(path.join(root, "components/frame")).filter((f) => /\.(tsx?|css)$/.test(f)).map((f) => path.join(root, "components/frame", f)),
      path.join(root, "lib/nav.ts"),
    ].filter((f) => !f.endsWith("frame.acceptance.test.tsx"));
    expect(files.length).toBeGreaterThan(10);
    const hits = files.filter((f) => /mastra/i.test(fs.readFileSync(f, "utf8")));
    expect(hits).toEqual([]);
  });

  test("the rendered frame shows only the product name", () => {
    media = mockMatchMedia(true);
    render(<FrameHarness><Body /></FrameHarness>);
    expect(document.body.textContent).not.toMatch(/mastra/i);
    expect(within(sidebar()).getByText("AI Platform Studio")).toBeInTheDocument();
  });
});
