/**
 * SL.15 accessibility smoke: every main page rendered inside the real frame WITH data (populated
 * lists, a loaded entity), in the light and the dark theme. Each must show exactly one level-one
 * heading and pass axe with the page rules on (one <main>, one h1). The empty-API version of this
 * walk is SL.4's `components/frame/frame.acceptance.test.tsx`; overlays (Sheets, popovers, the
 * palette) have their own axe tests next to their components.
 *
 * Fixtures reuse the shared fixture modules (metrics, workflow specs, processor catalog, models);
 * the API is the same `fetch` stub the frame acceptance test uses, answering with rows.
 */
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import React from "react";
import fs from "node:fs";
import path from "node:path";

const nav = vi.hoisted(() => ({ path: "/agents", params: {} as Record<string, string>, search: "" }));

vi.mock("next/navigation", () => ({
  usePathname: () => nav.path,
  useParams: () => nav.params,
  useRouter: () => ({ push: vi.fn(), replace: vi.fn(), back: vi.fn(), prefetch: vi.fn() }),
  useSearchParams: () => new URLSearchParams(nav.search),
}));

import { AXE_TIMEOUT_MS } from "./a11y";
import { expectAccessiblePage } from "./a11yPages";
import { respond } from "./pageFixtures";
import { FrameHarness, mockMatchMedia } from "@/components/frame/frameTesting";

beforeEach(() => {
  window.localStorage.clear();
  Object.defineProperty(window, "innerWidth", { configurable: true, writable: true, value: 1440 });
  vi.stubEnv("NEXT_PUBLIC_EAP_DEV_TOKEN", "test-token");
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: unknown) => {
      const url = new URL(String(typeof input === "string" ? input : (input as Request).url), "http://localhost");
      return new Response(JSON.stringify(respond(url)), { status: 200, headers: { "content-type": "application/json" } });
    }),
  );
  // EventSource-backed run streams are not opened by any page at rest; a stub keeps one from throwing.
  vi.stubGlobal("EventSource", class { close() {} addEventListener() {} removeEventListener() {} });
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.unstubAllEnvs();
});

interface PageCase {
  name: string;
  path: string;
  params?: Record<string, string>;
  /** The URL's query string (`tab=sources`). */
  search?: string;
  load: () => Promise<{ default: React.ComponentType }>;
  /** The visible h1. */
  h1: string;
  /** Text (or a test id with `#`) that shows the fixtures rendered, not an empty or error state. */
  shows: string;
}

/** An agent tab page inside `app/agents/[id]/layout.tsx`, as Next renders it. */
async function inAgentLayout(tab: Promise<{ default: React.ComponentType }>): Promise<{ default: React.ComponentType }> {
  const [{ default: Layout }, { default: Tab }] = await Promise.all([import("@/app/agents/[id]/layout"), tab]);
  return { default: () => <Layout><Tab /></Layout> };
}

const PAGES: PageCase[] = [
  { name: "agents", path: "/agents", load: () => import("@/app/agents/page"), h1: "Agents", shows: "Support bot" },
  { name: "agents/new", path: "/agents/new", load: () => import("@/app/agents/new/page"), h1: "New Agent", shows: "Identity" },
  { name: "agent chat", path: "/agents/a1/chat", params: { id: "a1" }, load: () => inAgentLayout(import("@/app/agents/[id]/chat/page")), h1: "Support bot", shows: "Refund question" },
  { name: "agent conversation", path: "/agents/a1/chat/c1", params: { id: "a1", conversationId: "c1" }, load: () => inAgentLayout(import("@/app/agents/[id]/chat/[conversationId]/page")), h1: "Support bot", shows: "hello" },
  { name: "agent traces", path: "/agents/a1/traces", params: { id: "a1" }, load: () => inAgentLayout(import("@/app/agents/[id]/traces/page")), h1: "Support bot", shows: "#agent-trace-row" },
  { name: "agent editor", path: "/agents/a1/editor", params: { id: "a1" }, load: () => inAgentLayout(import("@/app/agents/[id]/editor/page")), h1: "Support bot", shows: "Identity" },
  { name: "agent tests", path: "/agents/a1/tests", params: { id: "a1" }, load: () => inAgentLayout(import("@/app/agents/[id]/tests/page")), h1: "Support bot", shows: "Refund suite" },
  { name: "agent access", path: "/agents/a1/access", params: { id: "a1" }, load: () => inAgentLayout(import("@/app/agents/[id]/access/page")), h1: "Support bot", shows: "Fine-grained access is off" },
  { name: "agent memory", path: "/agents/a2/memory", params: { id: "a2" }, load: () => inAgentLayout(import("@/app/agents/[id]/memory/page")), h1: "Memory bot", shows: "#memory-resource-option" },
  { name: "workflows", path: "/workflows", load: () => import("@/app/workflows/page"), h1: "Workflows", shows: "Triage" },
  { name: "workflow schedules", path: "/workflows/schedules", load: () => import("@/app/workflows/schedules/page"), h1: "Schedules", shows: "No schedules" },
  { name: "workflow schedules tab", path: "/workflows/w1/schedules", params: { id: "w1" }, load: () => import("@/app/workflows/[id]/schedules/page"), h1: "Triage", shows: "No schedules" },
  { name: "workflow graph", path: "/workflows/w1", params: { id: "w1" }, load: () => import("@/app/workflows/[id]/page"), h1: "Triage", shows: "classify" },
  { name: "workflow tests tab", path: "/workflows/w1/tests", params: { id: "w1" }, load: () => import("@/app/workflows/[id]/tests/page"), h1: "Triage", shows: "#eval-suite-row" },
  { name: "workflow traces", path: "/workflows/w1/traces", params: { id: "w1" }, load: () => import("@/app/workflows/[id]/traces/page"), h1: "Triage", shows: "#workflow-trace-row" },
  { name: "workflow run", path: "/workflows/w1/runs/wr1", params: { id: "w1", runId: "wr1" }, load: () => import("@/app/workflows/[id]/runs/[runId]/page"), h1: "wr1", shows: "classify" },
  { name: "workflow run paused", path: "/workflows/w1/runs/wr2", params: { id: "w1", runId: "wr2" }, load: () => import("@/app/workflows/[id]/runs/[runId]/page"), h1: "wr2", shows: "#workflow-paused-banner" },
  { name: "architect", path: "/architect", load: () => import("@/app/architect/page"), h1: "Architect", shows: "FAQ bot" },
  { name: "tools", path: "/tools", load: () => import("@/app/tools/page"), h1: "Tools", shows: "Current time" },
  { name: "mcp", path: "/mcp", load: () => import("@/app/mcp/page"), h1: "MCP Servers", shows: "Docs" },
  { name: "mcp catalog", path: "/mcp/browse", load: () => import("@/app/mcp/browse/page"), h1: "Catalog", shows: "Files" },
  { name: "mcp detail", path: "/mcp/m1", params: { id: "m1" }, load: () => import("@/app/mcp/[id]/page"), h1: "Docs", shows: "https://docs.example.test/mcp" },
  { name: "knowledge", path: "/knowledge", load: () => import("@/app/knowledge/page"), h1: "Knowledge", shows: "Product docs" },
  { name: "knowledge base", path: "/knowledge/kb1", params: { id: "kb1" }, load: () => import("@/app/knowledge/[id]/page"), h1: "Product docs", shows: "#knowledge-document-row" },
  { name: "knowledge base sources", path: "/knowledge/kb1", params: { id: "kb1" }, search: "tab=sources", load: () => import("@/app/knowledge/[id]/page"), h1: "Product docs", shows: "TI parts" },
  { name: "processors", path: "/processors", load: () => import("@/app/processors/page"), h1: "Processors", shows: "#processor-row" },
  { name: "processor detail", path: "/processors/pii_redact", params: { type: "pii_redact" }, load: () => import("@/app/processors/[type]/page"), h1: "PII redaction", shows: "#result-row" },
  { name: "metrics", path: "/metrics", load: () => import("@/app/metrics/page"), h1: "Metrics", shows: "#recent-failure" },
  { name: "threads", path: "/threads", load: () => import("@/app/threads/page"), h1: "Threads", shows: "Refund question" },
  { name: "traces", path: "/traces", load: () => import("@/app/traces/page"), h1: "Traces", shows: "#trace-row" },
  { name: "spans", path: "/traces/spans", load: () => import("@/app/traces/spans/page"), h1: "Traces", shows: "#span-row" },
  { name: "logs", path: "/logs", load: () => import("@/app/logs/page"), h1: "Logs", shows: "tool failed" },
  { name: "approvals", path: "/approvals", load: () => import("@/app/approvals/page"), h1: "Approvals", shows: "send_email" },
  { name: "datasets", path: "/datasets", load: () => import("@/app/datasets/page"), h1: "Test suites", shows: "Refund suite" },
  { name: "dataset detail", path: "/datasets/s1", params: { id: "s1" }, load: () => import("@/app/datasets/[id]/page"), h1: "Refund suite", shows: "#eval-case-row" },
  { name: "scorers", path: "/scorers", load: () => import("@/app/scorers/page"), h1: "Scorers", shows: "#scorer-row" },
  { name: "experiments", path: "/experiments", load: () => import("@/app/experiments/page"), h1: "Test runs", shows: "#experiment-row" },
  { name: "webhooks", path: "/webhooks", load: () => import("@/app/webhooks/page"), h1: "Webhooks", shows: "#webhook-row" },
  { name: "external access", path: "/external-access", load: () => import("@/app/external-access/page"), h1: "External access", shows: "#external-export-row" },
  { name: "settings", path: "/settings", load: () => import("@/app/settings/page"), h1: "Settings", shows: "Appearance" },
  { name: "settings profile", path: "/settings", search: "tab=profile", load: () => import("@/app/settings/page"), h1: "Settings", shows: "My recent activity" },
  { name: "settings providers", path: "/settings", search: "tab=providers", load: () => import("@/app/settings/page"), h1: "Settings", shows: "LiteLLM gateway" },
  { name: "settings connections", path: "/settings", search: "tab=connections", load: () => import("@/app/settings/page"), h1: "Settings", shows: "catalogue-prod" },
  { name: "settings browser data", path: "/settings", search: "tab=browser-data", load: () => import("@/app/settings/page"), h1: "Settings", shows: "Data in this browser" },
];

describe("PAGES covers every page route in app/", () => {
  const APP = path.resolve(__dirname, "..", "app");
  /** `app/agents/[id]/page.tsx` → `/agents/:id`. */
  const patternOf = (file: string) => {
    const dir = path.dirname(path.relative(APP, file)).split(path.sep).join("/");
    return dir === "." ? "/" : `/${dir.replace(/\[(\w+)\]/g, ":$1")}`;
  };
  // /auth/callback renders nothing: the AuthGate in the root layout completes the OIDC sign-in there.
  // /setup renders nothing either: the AuthGate shows the setup page there (components/auth/LocalSignIn.test.tsx).
  const live: string[] = [];
  const walk = (dir: string) => {
    for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
      const full = path.join(dir, entry.name);
      if (entry.isDirectory()) walk(full);
      else if (entry.name === "page.tsx" && !/\bredirect\(/.test(fs.readFileSync(full, "utf8")) && patternOf(full) !== "/auth/callback" && patternOf(full) !== "/setup") live.push(patternOf(full));
    }
  };
  walk(APP);

  /** A PAGES path with each fixture param value turned back into `:name` (`/mcp/m1` → `/mcp/:id`). */
  const patternOfCase = (page: PageCase) =>
    Object.entries(page.params ?? {}).reduce((acc, [name, value]) => acc.replace(`/${value}`, `/:${name}`), page.path);

  test("every route that renders (not a redirect page) has a PAGES case, and every case is a route", () => {
    expect([...new Set(PAGES.map(patternOfCase))].sort()).toEqual([...live].sort());
  });
});

async function shown(marker: string): Promise<void> {
  if (marker.startsWith("#")) await screen.findAllByTestId(marker.slice(1), {}, { timeout: 4000 });
  else await waitFor(() => expect(within(screen.getByRole("main")).getAllByText(marker, { exact: false }).length).toBeGreaterThan(0), { timeout: 4000 });
}

describe("every main page with fixtures, inside the frame, light and dark", () => {
  test.each(PAGES)("$name: one h1, no axe violations", async (page) => {
    nav.path = page.path;
    nav.params = page.params ?? {};
    nav.search = page.search ?? "";
    const Page = (await page.load()).default;
    await expectAccessiblePage(() => <Page />, {
      ready: async () => {
        await shown(page.shows);
        const main = screen.getByRole("main");
        await waitFor(() => expect(within(main).getByRole("heading", { level: 1 })).toHaveTextContent(page.h1));
        expect(within(main).queryByTestId("datalist-error")).toBeNull();
      },
    });
  }, AXE_TIMEOUT_MS);
});

describe("expectAccessiblePage catches a broken page", () => {
  const ready = () => screen.findAllByRole("heading", { level: 1 });

  test("two h1s fail", async () => {
    const Two = () => <><h1>One</h1><h1>Two</h1></>;
    await expect(expectAccessiblePage(() => <Two />, { ready, themes: ["light"] })).rejects.toThrow(/one h1 in light/);
  }, AXE_TIMEOUT_MS);

  test("an axe violation fails (an unlabelled button)", async () => {
    const Bad = () => <><h1>Bad</h1><button type="button"><svg aria-hidden="true" /></button></>;
    await expect(expectAccessiblePage(() => <Bad />, { ready, themes: ["dark"] })).rejects.toThrow(/button-name/);
  }, AXE_TIMEOUT_MS);
});

/** Tab stops in DOM order (an approximation of the browser's sequential focus order without positive tabindex). */
function tabStops(): HTMLElement[] {
  const selector = 'a[href], button:not([disabled]), input:not([disabled]):not([type="hidden"]), select:not([disabled]), textarea:not([disabled]), summary, [tabindex]';
  return Array.from(document.querySelectorAll<HTMLElement>(selector)).filter((el) => el.tabIndex >= 0 && !el.closest("[hidden], [inert], [aria-hidden='true']"));
}

/** SL.15 focus-order fixes, pinned. */
describe("focus order", () => {
  afterEach(() => cleanup());

  test("no positive tabindex anywhere on a page; the skip link is the first tab stop, then the sidebar, then main", async () => {
    const media = mockMatchMedia(true);
    try {
      nav.path = "/agents";
      nav.search = "";
      const Page = (await import("@/app/agents/page")).default;
      render(<FrameHarness><Page /></FrameHarness>);
      await screen.findAllByText("Support bot");
      const stops = tabStops();
      expect(stops.filter((el) => el.tabIndex > 0)).toEqual([]);
      expect(stops[0]).toHaveTextContent("Skip to content");
      const sidebar = screen.getByTestId("sidebar");
      const main = screen.getByRole("main");
      const firstInSidebar = stops.findIndex((el) => sidebar.contains(el));
      const firstInMain = stops.findIndex((el) => main.contains(el));
      expect(firstInSidebar).toBeGreaterThan(0);
      expect(firstInMain).toBeGreaterThan(firstInSidebar);
    } finally {
      media.restore();
    }
  });

  test.each([
    ["workflow graph", "/workflows/w1", { id: "w1" }, () => import("@/app/workflows/[id]/page")],
    ["workflow run", "/workflows/w1/runs/wr1", { id: "w1", runId: "wr1" }, () => import("@/app/workflows/[id]/runs/[runId]/page")],
  ] as const)("%s: the floating panel (visually left) comes before the canvas's zoom controls (visually bottom right)", async (_name, pathname, params, load) => {
    const media = mockMatchMedia(true);
    try {
      nav.path = pathname;
      nav.search = "";
      nav.params = { ...params };
      const Page = (await load()).default;
      render(<FrameHarness><Page /></FrameHarness>);
      const panel = await screen.findByTestId("workflow-panel");
      const zoomIn = await screen.findByTestId("workflow-zoom-in");
      const stops = tabStops();
      const firstInPanel = stops.findIndex((el) => panel.contains(el));
      expect(firstInPanel).toBeGreaterThanOrEqual(0);
      expect(firstInPanel).toBeLessThan(stops.indexOf(zoomIn));
    } finally {
      media.restore();
    }
  });
});
