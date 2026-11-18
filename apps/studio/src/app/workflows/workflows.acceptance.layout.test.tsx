/**
 * SL.14 acceptance, layout half: tabs and deep links, the floating panel's fit rule at real window
 * sizes, persistence and Settings, the phone-width Sheet, the Timeline dock, the approval overlay,
 * the trace Sheets and axe in both themes. The pages render alone (stateful router) or inside the
 * app frame (`FrameHarness`) where the canvas width depends on the sidebar.
 */
import fs from "fs";
import path from "path";
import React from "react";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import type { Run, RunEvent, SubscribeHandlers } from "@/lib/runs";
import { demoSpec } from "@/test/workflowFixtures";
import { expectNoAxeViolations, AXE_TIMEOUT_MS } from "@/test/a11y";
import { FrameHarness, mockMatchMedia, setWindowWidth, type MatchMediaControl } from "@/components/frame/frameTesting";
import { routes } from "@/lib/routes";
import { SIDEBAR_ATTRIBUTE, SIDEBAR_STORAGE_KEY } from "@/lib/sidebarPref";
import { workflowRunLedger } from "@/lib/workflows";
import {
  panelMaxWidth, PANEL_DEFAULT_WIDTH, PANEL_KEY_STEP, PANEL_MIN_WIDTH, TIMELINE_DEFAULT_HEIGHT, TIMELINE_MAX_HEIGHT, TIMELINE_MIN_HEIGHT,
  WORKFLOW_LAYOUT_STORAGE_KEY, workflowCanvasWidth,
} from "@/components/workflows/workflowLayout";

// A stateful router: push adds a history entry, replace rewrites it, back pops it.
const nav = vi.hoisted(() => ({
  path: "", search: "", entries: [] as string[], params: {} as Record<string, string>,
  listeners: new Set<() => void>(), push: vi.fn(), replace: vi.fn(), back: vi.fn(),
}));
function setUrl(url: string) {
  const q = url.indexOf("?");
  nav.path = q === -1 ? url : url.slice(0, q);
  nav.search = q === -1 ? "" : url.slice(q + 1);
  nav.listeners.forEach((listener) => listener());
}
nav.push.mockImplementation((url: string) => { nav.entries.push(url); setUrl(url); });
nav.replace.mockImplementation((url: string) => { nav.entries[nav.entries.length - 1] = url; setUrl(url); });
nav.back.mockImplementation(() => { if (nav.entries.length > 1) nav.entries.pop(); setUrl(nav.entries[nav.entries.length - 1]); });

vi.mock("next/navigation", async () => {
  const React = await import("react");
  return {
    useParams: () => nav.params,
    usePathname: () => nav.path,
    useRouter: () => ({ push: nav.push, replace: nav.replace, back: nav.back, prefetch: vi.fn() }),
    useSearchParams: () => {
      const search = React.useSyncExternalStore((cb) => { nav.listeners.add(cb); return () => nav.listeners.delete(cb); }, () => nav.search, () => nav.search);
      return React.useMemo(() => new URLSearchParams(search), [search]);
    },
  };
});
vi.mock("next/link", () => ({ default: ({ href, children, ...rest }: { href: string; children: React.ReactNode }) => <a href={href} {...rest}>{children}</a> }));
const runsMock = vi.hoisted(() => ({ getRun: vi.fn(), listEvents: vi.fn(), subscribeRun: vi.fn() }));
vi.mock("@/lib/runs", async (importOriginal) => ({ ...(await importOriginal<object>()), ...runsMock }));

import WorkflowsPage from "./page";
import WorkflowDetailPage from "./[id]/page";
import WorkflowTracesPage from "./[id]/traces/page";
import WorkflowRunPage from "./[id]/runs/[runId]/page";
import SettingsPage from "@/app/settings/page";

const fetchMock = vi.fn();
const json = (status: number, body: unknown) => Promise.resolve(new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } }));
const WF = { id: "wf-1", name: "Triage", draft_spec: demoSpec, latest_version: 2 };
const V2 = { workflow_id: "wf-1", version: 2, spec: demoSpec, spec_hash: "h2", created_at: "2026-11-13T09:00:00Z", created_by: "dev" };
const T = (s: number) => new Date(Date.UTC(2026, 10, 13, 10, 0, s)).toISOString();
const RUN_ROW = (id: string, minute: number) => ({
  run_id: id, kind: "workflow", workflow_id: "wf-1", workflow_version: 2, display_status: "completed", status: "completed",
  created_at: `2026-11-13T10:0${minute}:00Z`, updated_at: `2026-11-13T10:0${minute}:02Z`, input_tokens: 10, output_tokens: 5, cost_usd: null,
});
const RUN: Run = {
  run_id: "run-1", kind: "workflow", workflow_id: "wf-1", workflow_version: 2, agent_id: null, version: null, status: "running",
  display_status: "running", input: "{}", output: null, error: null, heartbeat_at: null, created_at: T(0), updated_at: T(5),
  trace_id: null, conversation_id: null, turn: null,
};

function traceOf(runId: string) {
  const span = (id: string, parent: string | null, kind: string, name: string, attributes: Record<string, unknown> = {}) =>
    ({ id, parent_id: parent, kind, name, start: T(0), end: T(5), status: "ok", attributes, input: null, output: null, truncated: {} });
  return {
    run: { ...RUN, run_id: runId, status: "completed", display_status: "completed" },
    spans: [span("root", null, "run", `run ${runId}`, { run_kind: "workflow", run_id: runId })],
    totals: { runs: 1, model_requests: 0, tool_calls: 0, input_tokens: null, output_tokens: null, cache_read_tokens: null, cache_write_tokens: null,
      cost_usd: null, unpriced_requests: 0, unrecorded_requests: 0, duration_ms: 5000, status: "completed" },
    truncated: { spans: false, events: false, child_runs: false, payload_fields: 0 },
    limits: { max_spans: 2000, max_events_per_run: 10_000, max_child_runs: 500, field_max_bytes: 8192, payload_budget_bytes: 2097152 },
  };
}

let posts: Array<{ path: string; body: unknown }> = [];
function route(url: string, init?: RequestInit): Promise<Response> {
  const u = new URL(url);
  const p = u.pathname;
  if (init?.method === "POST") {
    posts.push({ path: p, body: init.body ? JSON.parse(String(init.body)) : null });
    if (p === "/api/v1/workflows/wf-1/runs") return json(202, { run_id: "run-new", kind: "workflow", workflow_id: "wf-1", version: 2, status: "queued", display_status: "queued" });
    if (p.startsWith("/api/v1/workflows/runs/run-1/steps/approve/")) return json(200, { run_id: "run-1", step_id: "approve", decision: "approved", decided_by: "ada", comment: null, notified: true });
  }
  if (p === "/api/v1/workflows") return json(200, [WF, { id: "wf-2", name: "Other", draft_spec: demoSpec, latest_version: null }]);
  if (p === "/api/v1/workflows/wf-1") return json(200, WF);
  if (p === "/api/v1/workflows/wf-1/versions") return json(200, [V2]);
  if (p === "/api/v1/workflows/wf-1/versions/2") return json(200, V2);
  if (p === "/api/v1/workflows/wf-2") return json(200, { id: "wf-2", name: "Other", draft_spec: demoSpec, latest_version: null });
  if (p === "/api/v1/workflows/wf-2/versions") return json(200, []);
  if (p === "/api/v1/runs") return json(200, { items: [RUN_ROW("run-b", 2), RUN_ROW("run-a", 1)], next_before: null });
  const trace = p.match(/^\/api\/v1\/runs\/([^/]+)\/trace$/);
  if (trace) return json(200, traceOf(trace[1]));
  return json(404, { detail: "not mocked" });
}

let seq = 0;
let stored: RunEvent[] = [];
let handlers: SubscribeHandlers = {};
const ev = (type: RunEvent["type"], data: Record<string, unknown> = {}, at = 0): RunEvent => ({ seq: seq++, type, data, ts: T(at) });
const emit = (...events: RunEvent[]) => act(() => events.forEach((event) => handlers.onEvent?.(event)));

let restoreWidth: () => void = () => undefined;
let media: MatchMediaControl | null = null;

beforeAll(() => {
  class RO { observe() {} unobserve() {} disconnect() {} }
  vi.stubGlobal("ResizeObserver", RO);
  vi.stubGlobal("DOMMatrixReadOnly", class { m22 = 1; });
});

function go(url: string, params: Record<string, string> = { id: "wf-1" }) {
  nav.params = params;
  nav.entries = [url];
  setUrl(url);
}

beforeEach(() => {
  vi.stubEnv("NEXT_PUBLIC_EAP_DEV_TOKEN", "Bearer test");
  document.documentElement.lang = "en";
  document.documentElement.removeAttribute(SIDEBAR_ATTRIBUTE);
  document.documentElement.classList.remove("dark");
  posts = [];
  seq = 0;
  stored = [];
  handlers = {};
  fetchMock.mockReset();
  fetchMock.mockImplementation((url: string, init?: RequestInit) => route(String(url), init));
  vi.stubGlobal("fetch", fetchMock);
  for (const fn of [nav.push, nav.replace, nav.back]) fn.mockClear();
  runsMock.getRun.mockImplementation(async (id: string) => ({ ...RUN, run_id: id }));
  runsMock.listEvents.mockImplementation(async () => stored);
  runsMock.subscribeRun.mockImplementation((_id: string, next: SubscribeHandlers) => { handlers = next; return () => undefined; });
  workflowRunLedger.reset();
  localStorage.clear();
  go(routes.workflow("wf-1"));
});

afterEach(() => {
  cleanup();
  restoreWidth();
  restoreWidth = () => undefined;
  media?.restore();
  media = null;
  document.documentElement.removeAttribute(SIDEBAR_ATTRIBUTE);
  document.documentElement.classList.remove("dark");
  vi.unstubAllEnvs();
});

/** jsdom has no PointerEvent: a MouseEvent of the pointer type carries clientX and clientY to React. */
const pointer = (el: Element, type: "pointerdown" | "pointermove" | "pointerup", clientX: number, clientY = 0) =>
  fireEvent(el, new MouseEvent(type, { bubbles: true, cancelable: true, button: 0, clientX, clientY }));
const saved = () => JSON.parse(localStorage.getItem(WORKFLOW_LAYOUT_STORAGE_KEY) ?? "{}") as Record<string, unknown>;
const tabs = () => screen.getByRole("navigation", { name: "Workflow sections" });
const resize = () => screen.getByTestId("workflow-panel-resize");
const width = () => Number(resize().getAttribute("aria-valuenow"));
const max = () => Number(resize().getAttribute("aria-valuemax"));

async function graphTab() {
  render(<WorkflowDetailPage />);
  await screen.findByTestId("workflow-graph");
  await screen.findByTestId("workflow-run-row-run-b");
}
async function runView(url = routes.workflowRun("wf-1", "run-1")) {
  go(url, { id: "wf-1", runId: "run-1" });
  render(<WorkflowRunPage />);
  // The graph node, not the panel's step list: the list is inside the Sheet at phone width.
  await screen.findByTestId("workflow-node-classify");
}

describe("list, tabs and deep links", () => {
  it("the list has one h1 and its rows open the Graph tab", async () => {
    go(routes.workflows(), {});
    render(<WorkflowsPage />);
    const list = await screen.findByTestId("workflows-list");
    expect(screen.getAllByRole("heading", { level: 1 })).toHaveLength(1);
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent("Workflows");
    await within(list).findByTestId("workflow-row-wf-1");
    expect(within(list).getByRole("link", { name: "Triage" })).toHaveAttribute("href", routes.workflow("wf-1"));
  });

  it("the Graph tab is the default and carries the three tab test ids", async () => {
    await graphTab();
    expect(screen.getByTestId("workflow-tab-graph")).toHaveAttribute("aria-current", "page");
    expect(screen.getByTestId("workflow-tab-runs")).not.toHaveAttribute("aria-current");
    expect(screen.getByTestId("workflow-tab-runs")).toHaveAttribute("href", routes.workflow("wf-1", { tab: "runs" }));
    expect(screen.getByTestId("workflow-tab-traces")).toHaveAttribute("href", routes.workflowTraces("wf-1"));
    expect(screen.getAllByRole("heading", { level: 1 })).toHaveLength(1);
  });

  it("?tab=runs deep-links to the Runs tab, an unknown tab falls back to Graph", async () => {
    go(routes.workflow("wf-1", { tab: "runs" }));
    render(<WorkflowDetailPage />);
    await screen.findByTestId("workflow-run-history");
    expect(screen.getByTestId("workflow-tab-runs")).toHaveAttribute("aria-current", "page");
    expect(screen.queryByTestId("workflow-graph")).not.toBeInTheDocument();
    expect(screen.queryByTestId("workflow-panel")).not.toBeInTheDocument();
    cleanup();
    go(`${routes.workflow("wf-1")}?tab=bogus`);
    render(<WorkflowDetailPage />);
    expect(await screen.findByTestId("workflow-graph")).toBeInTheDocument();
    expect(screen.getByTestId("workflow-tab-graph")).toHaveAttribute("aria-current", "page");
  });

  it("the All runs link switches from Graph to the Runs tab", async () => {
    await graphTab();
    expect(screen.getByTestId("workflow-all-runs")).toHaveAttribute("href", routes.workflow("wf-1", { tab: "runs" }));
  });

  it("/traces is the Traces tab with its runs listed", async () => {
    go(routes.workflowTraces("wf-1"));
    render(<WorkflowTracesPage />);
    const list = await screen.findByTestId("workflow-traces-list");
    await waitFor(() => expect(within(list).getAllByTestId("workflow-trace-row")).toHaveLength(2));
    expect(screen.getByTestId("workflow-tab-traces")).toHaveAttribute("aria-current", "page");
    expect(screen.getByTestId("workflow-tab-graph")).not.toHaveAttribute("aria-current");
    expect(screen.getAllByRole("heading", { level: 1 })).toHaveLength(1);
  });

  it.each([
    ["Graph", routes.workflow("wf-1"), routes.workflow("wf-2")],
    ["Runs", routes.workflow("wf-1", { tab: "runs" }), routes.workflow("wf-2", { tab: "runs" })],
  ])("the switcher keeps the %s tab", async (_name, url, target) => {
    go(url);
    render(<WorkflowDetailPage />);
    await screen.findByTestId("workflow-tab-graph");
    fireEvent.click(await screen.findByTestId("workflow-switcher"));
    fireEvent.click(await screen.findByRole("option", { name: "Other" }));
    expect(nav.push).toHaveBeenCalledWith(target);
  });

  it("the switcher keeps the Traces tab", async () => {
    go(routes.workflowTraces("wf-1"));
    render(<WorkflowTracesPage />);
    fireEvent.click(await screen.findByTestId("workflow-switcher"));
    fireEvent.click(await screen.findByRole("option", { name: "Other" }));
    expect(nav.push).toHaveBeenCalledWith(routes.workflowTraces("wf-2"));
  });
});

async function framedGraph(viewport: number, sidebarCollapsed: boolean) {
  media = mockMatchMedia(true);
  restoreWidth = setWindowWidth(viewport);
  // The frame treats storage as the truth (and mirrors it to <html data-sidebar>).
  if (sidebarCollapsed) localStorage.setItem(SIDEBAR_STORAGE_KEY, "collapsed");
  render(<FrameHarness><WorkflowDetailPage /></FrameHarness>);
  await screen.findByTestId("workflow-graph");
  await screen.findByTestId("workflow-panel-resize");
}

describe("floating panel: resize and the fit rule", () => {
  it("the default is 380 px; arrow keys move by 16 px, Home and End jump to the limits", async () => {
    await graphTab();
    expect(width()).toBe(PANEL_DEFAULT_WIDTH);
    expect(screen.getByTestId("workflow-panel")).toHaveAttribute("data-collapsed", "false");
    fireEvent.keyDown(resize(), { key: "ArrowLeft" });
    expect(width()).toBe(PANEL_DEFAULT_WIDTH - PANEL_KEY_STEP);
    fireEvent.keyDown(resize(), { key: "Home" });
    expect(width()).toBe(PANEL_MIN_WIDTH);
    fireEvent.keyDown(resize(), { key: "ArrowLeft" });
    expect(width()).toBe(PANEL_MIN_WIDTH);
    expect(screen.getByTestId("workflow-panel")).toHaveStyle({ width: `${PANEL_MIN_WIDTH}px` });
    expect(resize()).toHaveAttribute("tabindex", "0");
    expect(saved()).toMatchObject({ panelWidth: PANEL_MIN_WIDTH });
  });

  it("a pointer drag resizes live, clamps at both limits and saves on release only", async () => {
    await graphTab();
    pointer(resize(), "pointerdown", 400);
    pointer(resize(), "pointermove", 440);
    expect(width()).toBe(PANEL_DEFAULT_WIDTH + 40);
    expect(localStorage.getItem(WORKFLOW_LAYOUT_STORAGE_KEY)).toBeNull();
    pointer(resize(), "pointermove", 5000);
    expect(width()).toBe(max());
    pointer(resize(), "pointermove", -5000);
    expect(width()).toBe(PANEL_MIN_WIDTH);
    pointer(resize(), "pointermove", 420);
    pointer(resize(), "pointerup", 420);
    expect(width()).toBe(PANEL_DEFAULT_WIDTH + 20);
    expect(saved()).toMatchObject({ panelWidth: PANEL_DEFAULT_WIDTH + 20 });
    // After release a stray move does nothing.
    pointer(resize(), "pointermove", 900);
    expect(width()).toBe(PANEL_DEFAULT_WIDTH + 20);
  });

  it.each([
    // viewport, sidebar rail, expected maximum (canvas - 2 x 12 px inset - 240 px of graph, capped at 560)
    [1024, false, 510],
    [1024, true, 560],
    [1280, false, 560],
    [1280, true, 560],
    [1440, false, 560],
    [1440, true, 560],
  ])("in the frame at %i px (sidebar rail: %s) the panel stops at %i px and leaves 240 px of graph", async (viewport, rail, expected) => {
    await framedGraph(viewport, rail);
    const canvas = workflowCanvasWidth({ viewport, sidebarCollapsed: rail, inFrame: true });
    expect(max()).toBe(expected);
    expect(max()).toBe(panelMaxWidth(canvas));
    fireEvent.keyDown(resize(), { key: "End" });
    expect(width()).toBe(expected);
    fireEvent.keyDown(resize(), { key: "ArrowRight" });
    expect(width()).toBe(expected);
    expect(canvas - 24 - width()).toBeGreaterThanOrEqual(240);
    expect(screen.getByTestId("workflow-panel")).toHaveStyle({ width: `${expected}px` });
  });

  it("a saved width wider than the window allows is shown clamped, and the saved value is untouched", async () => {
    localStorage.setItem(WORKFLOW_LAYOUT_STORAGE_KEY, JSON.stringify({ panelWidth: 560, panelCollapsed: false, timelineOpen: false, timelineHeight: 240 }));
    await framedGraph(1024, false);
    await waitFor(() => expect(width()).toBe(510));
    expect(saved()).toMatchObject({ panelWidth: 560 });
  });

  it("garbage in storage falls back to the defaults field by field", async () => {
    localStorage.setItem(WORKFLOW_LAYOUT_STORAGE_KEY, JSON.stringify({ panelWidth: "wide", panelCollapsed: "yes", timelineOpen: true, timelineHeight: 9999 }));
    await runView();
    expect(width()).toBe(PANEL_DEFAULT_WIDTH);
    expect(screen.getByTestId("workflow-panel")).toHaveAttribute("data-collapsed", "false");
    expect(screen.getByTestId("workflow-timeline-toggle")).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByTestId("workflow-timeline-resize")).toHaveAttribute("aria-valuenow", String(TIMELINE_MAX_HEIGHT));
  });
});

describe("collapse, persistence and Settings", () => {
  it("collapses to a 40 px rail, restores, and keeps the state across a reload on both pages", async () => {
    await graphTab();
    fireEvent.click(screen.getByTestId("workflow-panel-toggle"));
    const rail = screen.getByTestId("workflow-panel");
    expect(rail).toHaveAttribute("data-collapsed", "true");
    expect(rail).toHaveStyle({ width: "40px" });
    expect(screen.queryByTestId("workflow-panel-body")).not.toBeInTheDocument();
    expect(screen.queryByTestId("workflow-panel-resize")).not.toBeInTheDocument();
    expect(saved()).toMatchObject({ panelCollapsed: true, panelWidth: PANEL_DEFAULT_WIDTH });
    cleanup();

    // The run view shares the key: it opens collapsed too, and restoring there restores here.
    await runView();
    expect(screen.getByTestId("workflow-panel")).toHaveAttribute("data-collapsed", "true");
    fireEvent.click(screen.getByTestId("workflow-panel-toggle"));
    expect(screen.getByTestId("workflow-panel")).toHaveAttribute("data-collapsed", "false");
    expect(within(screen.getByTestId("workflow-panel-body")).getByTestId("workflow-run-summary")).toBeInTheDocument();
    cleanup();
    await graphTab();
    expect(screen.getByTestId("workflow-panel")).toHaveAttribute("data-collapsed", "false");
  });

  it("the width set by keys survives collapsing and restoring", async () => {
    await graphTab();
    fireEvent.keyDown(resize(), { key: "ArrowRight" });
    fireEvent.click(screen.getByTestId("workflow-panel-toggle"));
    fireEvent.click(screen.getByTestId("workflow-panel-toggle"));
    expect(width()).toBe(PANEL_DEFAULT_WIDTH + PANEL_KEY_STEP);
  });

  it("Settings' Clear layout preferences removes the workflow layout and the pages reopen at the defaults", async () => {
    media = mockMatchMedia(true);
    localStorage.setItem(WORKFLOW_LAYOUT_STORAGE_KEY, JSON.stringify({ panelWidth: 300, panelCollapsed: true, timelineOpen: true, timelineHeight: 400 }));
    localStorage.setItem("studio.theme", "light");
    go(routes.settingsTab("browser-data"), {});
    render(<FrameHarness><SettingsPage /></FrameHarness>);
    fireEvent.click(await screen.findByTestId("settings-clear-layout"));
    fireEvent.click(within(await screen.findByRole("dialog")).getByRole("button", { name: "Clear" }));
    await screen.findByTestId("settings-clear-status");
    expect(localStorage.getItem(WORKFLOW_LAYOUT_STORAGE_KEY)).toBeNull();
    expect(localStorage.getItem("studio.theme")).toBe("light");
    cleanup();

    await runView();
    expect(screen.getByTestId("workflow-panel")).toHaveAttribute("data-collapsed", "false");
    expect(width()).toBe(PANEL_DEFAULT_WIDTH);
    expect(screen.getByTestId("workflow-timeline-toggle")).toHaveAttribute("aria-expanded", "false");
  });

  it("Settings lists the workflow layout among the browser data it holds", async () => {
    media = mockMatchMedia(true);
    go(routes.settingsTab("browser-data"), {});
    render(<FrameHarness><SettingsPage /></FrameHarness>);
    expect(await screen.findByText("Workflow page layout")).toBeInTheDocument();
    expect(screen.getByText(/run Timeline is open and its height/)).toBeInTheDocument();
  });
});

describe("bottom Sheet below 768 px", () => {
  async function narrowGraph() {
    restoreWidth = setWindowWidth(375);
    render(<WorkflowDetailPage />);
    await screen.findByTestId("workflow-graph");
    fireEvent.click(screen.getByTestId("workflow-panel-open"));
    return screen.findByTestId("workflow-panel-sheet");
  }
  const open = () => screen.getByTestId("workflow-panel-open");

  it("replaces the floating panel with an Open button over a full-width canvas", async () => {
    restoreWidth = setWindowWidth(375);
    render(<WorkflowDetailPage />);
    await screen.findByTestId("workflow-graph");
    expect(screen.queryByTestId("workflow-panel")).not.toBeInTheDocument();
    expect(screen.queryByTestId("workflow-panel-resize")).not.toBeInTheDocument();
    expect(open()).toHaveAccessibleName("Show workflow");
    expect(screen.getByTestId("workflow-zoom-controls")).toBeInTheDocument();
  });

  it("opens the Sheet, closes it with Escape and returns focus to the Open button", async () => {
    restoreWidth = setWindowWidth(375);
    render(<WorkflowDetailPage />);
    await screen.findByTestId("workflow-graph");
    open().focus();
    fireEvent.click(open());
    const sheet = await screen.findByTestId("workflow-panel-sheet");
    expect(sheet).toHaveAccessibleName("Workflow");
    expect(within(sheet).getByTestId("workflow-run-panel")).toBeInTheDocument();
    expect(within(sheet).getByTestId("workflow-all-runs")).toBeInTheDocument();
    fireEvent.keyDown(sheet, { key: "Escape" });
    await waitFor(() => expect(screen.queryByTestId("workflow-panel-sheet")).not.toBeInTheDocument());
    await waitFor(() => expect(open()).toHaveFocus());
  });

  it("starts a run from the Sheet and goes to the run page", async () => {
    const sheet = await narrowGraph();
    await within(sheet).findByRole("button", { name: "Run workflow" });
    fireEvent.change(within(sheet).getByLabelText("Text *"), { target: { value: "refund please" } });
    fireEvent.click(within(sheet).getByRole("button", { name: "Run workflow" }));
    await waitFor(() => expect(nav.push).toHaveBeenCalledWith(routes.workflowRun("wf-1", "run-new")));
    expect(posts).toHaveLength(1);
    expect(posts[0].path).toBe("/api/v1/workflows/wf-1/runs");
    expect(posts[0].body).toMatchObject({ input: { text: "refund please" }, version: 2 });
  });

  it("typed Run input survives closing the Sheet with Escape and reopening it", async () => {
    const sheet = await narrowGraph();
    fireEvent.change(within(sheet).getByLabelText("Text *"), { target: { value: "draft text" } });
    fireEvent.click(within(sheet).getByRole("button", { name: "JSON" }));
    fireEvent.keyDown(sheet, { key: "Escape" });
    await waitFor(() => expect(screen.queryByTestId("workflow-panel-sheet")).not.toBeInTheDocument());
    expect(screen.queryByTestId("workflow-run-panel")).not.toBeInTheDocument();
    fireEvent.click(screen.getByTestId("workflow-panel-open"));
    const again = await screen.findByTestId("workflow-panel-sheet");
    // Same form: still in JSON mode, with the typed value.
    expect(within(again).getByRole("button", { name: "JSON" })).toHaveAttribute("aria-pressed", "true");
    expect(JSON.parse((within(again).getByTestId("workflow-input-json") as HTMLTextAreaElement).value)).toEqual({ text: "draft text" });
    fireEvent.click(within(again).getByRole("button", { name: "Form" }));
    expect(within(again).getByLabelText("Text *")).toHaveValue("draft text");
  });

  it("the run view's phone Sheet carries the run id and status,", async () => {
    restoreWidth = setWindowWidth(375);
    await runView();
    fireEvent.click(screen.getByTestId("workflow-panel-open"));
    const sheet = await screen.findByTestId("workflow-panel-sheet");
    expect(sheet).toHaveAccessibleName("Run details");
    expect(within(sheet).getByTestId("workflow-run-id")).toHaveTextContent("run-1");
    expect(within(sheet).getByTestId("workflow-run-duration")).toHaveTextContent("in progress");
  });
});

const dockHandle = () => screen.getByTestId("workflow-timeline-resize");
const dockHeight = () => Number(dockHandle().getAttribute("aria-valuenow"));
// Rows carry workflow-timeline-row-<stepId> (an allowlisted dynamic prefix in the test-id inventory).
const timelineItems = () => within(screen.getByTestId("workflow-timeline")).getAllByRole("listitem");
const rowStepId = (li: HTMLElement) => li.getAttribute("data-testid")!.replace(/^workflow-timeline-row-/, "");
const rowIds = () => timelineItems().map(rowStepId);
const timelineRow = (id: string) => timelineItems().find((li) => rowStepId(li) === id)!;

describe("Timeline dock", () => {
  it("is a 36 px bar when collapsed, expands to 240 px and persists across a reload", async () => {
    await runView();
    const dock = screen.getByTestId("workflow-timeline-dock");
    const toggle = screen.getByTestId("workflow-timeline-toggle");
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    expect(toggle.parentElement).toHaveStyle({ height: "36px" });
    expect(screen.queryByTestId("workflow-timeline")).not.toBeInTheDocument();
    expect(screen.queryByTestId("workflow-timeline-resize")).not.toBeInTheDocument();
    expect(dock).toHaveAttribute("data-open", "false");
    fireEvent.click(toggle);
    expect(dock).toHaveAttribute("data-open", "true");
    expect(dockHeight()).toBe(TIMELINE_DEFAULT_HEIGHT);
    expect(saved()).toMatchObject({ timelineOpen: true, timelineHeight: TIMELINE_DEFAULT_HEIGHT });
    cleanup();
    await runView();
    expect(screen.getByTestId("workflow-timeline-toggle")).toHaveAttribute("aria-expanded", "true");
    fireEvent.click(screen.getByTestId("workflow-timeline-toggle"));
    expect(saved()).toMatchObject({ timelineOpen: false });
  });

  it("resizes by keys within 120 to 480 px, and by a drag that grows upwards, saving on release", async () => {
    await runView();
    fireEvent.click(screen.getByTestId("workflow-timeline-toggle"));
    fireEvent.keyDown(dockHandle(), { key: "ArrowUp" });
    expect(dockHeight()).toBe(TIMELINE_DEFAULT_HEIGHT + 16);
    for (let i = 0; i < 40; i++) fireEvent.keyDown(dockHandle(), { key: "ArrowUp" });
    expect(dockHeight()).toBe(TIMELINE_MAX_HEIGHT);
    for (let i = 0; i < 40; i++) fireEvent.keyDown(dockHandle(), { key: "ArrowDown" });
    expect(dockHeight()).toBe(TIMELINE_MIN_HEIGHT);
    expect(saved()).toMatchObject({ timelineHeight: TIMELINE_MIN_HEIGHT });

    pointer(dockHandle(), "pointerdown", 0, 500);
    pointer(dockHandle(), "pointermove", 0, 450);
    expect(dockHeight()).toBe(TIMELINE_MIN_HEIGHT + 50);
    expect(saved()).toMatchObject({ timelineHeight: TIMELINE_MIN_HEIGHT });
    pointer(dockHandle(), "pointermove", 0, -9000);
    expect(dockHeight()).toBe(TIMELINE_MAX_HEIGHT);
    pointer(dockHandle(), "pointermove", 0, 470);
    pointer(dockHandle(), "pointerup", 0, 470);
    expect(dockHeight()).toBe(TIMELINE_MIN_HEIGHT + 30);
    expect(saved()).toMatchObject({ timelineHeight: TIMELINE_MIN_HEIGHT + 30 });
  });

  it("rows, states and durations follow the run's events, in the order the steps started", async () => {
    await runView();
    fireEvent.click(screen.getByTestId("workflow-timeline-toggle"));
    expect(screen.getByTestId("workflow-timeline-summary")).toHaveTextContent("0 of 5 steps done");
    emit(
      ev("step_started", { step_id: "classify", step_type: "agent" }, 0),
      ev("step_completed", { step_id: "classify", output: "billing" }, 2),
      ev("step_started", { step_id: "approve", step_type: "approval" }, 1),
      ev("step_waiting", { step_id: "approve", prompt: "Escalate?" }, 1),
      ev("step_started", { step_id: "lookup", step_type: "tool" }, 3),
      ev("step_failed", { step_id: "lookup", error: "boom" }, 5),
    );
    // Started order: classify (0 s), approve (1 s), lookup (3 s); then the steps that never started, in graph order.
    expect(rowIds()).toEqual(["classify", "approve", "lookup", "route", "summarize"]);
    const expected: Record<string, [string, string]> = {
      classify: ["completed", "2.00 s"], approve: ["waiting", "in progress"], lookup: ["failed", "2.00 s"], route: ["pending", "—"], summarize: ["pending", "—"],
    };
    for (const [id, [state, duration]] of Object.entries(expected)) {
      const row = timelineRow(id);
      expect(row, id).toHaveAttribute("data-state", state);
      expect(row, id).toHaveAttribute("data-state", screen.getByTestId(`workflow-node-${id}`).getAttribute("data-state")!);
      expect(within(row).getByTestId("workflow-timeline-duration"), id).toHaveTextContent(duration);
      expect(within(row).getByTestId("workflow-timeline-state"), id).toHaveTextContent(new RegExp(state, "i"));
    }
    expect(screen.getByTestId("workflow-timeline-summary")).toHaveTextContent("1 of 5 steps done");
  });

  it("keeps the summary visible when collapsed and picking a row selects the step", async () => {
    await runView();
    emit(ev("step_started", { step_id: "classify", step_type: "agent" }, 0), ev("step_completed", { step_id: "classify", output: "x" }, 1));
    expect(screen.getByTestId("workflow-timeline-summary")).toHaveTextContent("1 of 5 steps done");
    fireEvent.click(screen.getByTestId("workflow-timeline-toggle"));
    fireEvent.click(within(timelineRow("classify")).getByRole("button", { pressed: false }));
    expect(within(timelineRow("classify")).getByRole("button", { pressed: true })).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByTestId("step-io-panel")).toHaveAttribute("data-step-id", "classify");
  });
});

const waitForApproval = () => emit(ev("step_started", { step_id: "approve", step_type: "approval" }, 1), ev("step_waiting", { step_id: "approve", prompt: "Escalate?" }, 1));
const nodeButton = (id: string) => screen.getByRole("button", { name: new RegExp(`^Step ${id},`) });

describe("approval overlay", () => {
  it("floats in the right column with a Needs input badge only while a step waits", async () => {
    await runView();
    expect(screen.queryByTestId("workflow-approval-overlay")).not.toBeInTheDocument();
    expect(screen.queryByTestId("workflow-right-column")).not.toBeInTheDocument();
    waitForApproval();
    const overlay = screen.getByTestId("workflow-approval-overlay");
    expect(screen.getByTestId("workflow-right-column")).toContainElement(overlay);
    expect(screen.getByTestId("workflow-right-column")).not.toContainElement(screen.getByTestId("workflow-panel"));
    expect(overlay).toHaveAccessibleName("Waiting approvals");
    expect(within(overlay).getByTestId("workflow-needs-input")).toHaveTextContent("Needs input");
    expect(within(overlay).getByTestId("approval-prompt-approve")).toHaveTextContent("Escalate?");
    expect(screen.getByTestId("workflow-step-approve")).toHaveAttribute("data-state", "waiting");
  });

  it("approves from the overlay: one POST, the overlay goes, the step carries on and the timeline follows", async () => {
    await runView();
    waitForApproval();
    stored = [
      ev("step_started", { step_id: "approve", step_type: "approval" }, 1),
      ev("step_waiting", { step_id: "approve", prompt: "Escalate?" }, 1),
      ev("step_approved", { step_id: "approve", decided_by: "ada" }, 4),
      ev("step_completed", { step_id: "approve", output: { approved: true } }, 4),
    ];
    fireEvent.click(screen.getByTestId("workflow-timeline-toggle"));
    fireEvent.click(within(screen.getByTestId("workflow-approval-overlay")).getByRole("button", { name: "Approve" }));
    await waitFor(() => expect(screen.queryByTestId("workflow-approval-overlay")).not.toBeInTheDocument());
    expect(posts.map((p) => p.path)).toEqual(["/api/v1/workflows/runs/run-1/steps/approve/approve"]);
    expect(screen.getByTestId("workflow-step-approve")).toHaveAttribute("data-state", "completed");
    expect(timelineRow("approve")).toHaveAttribute("data-state", "completed");
    expect(within(timelineRow("approve")).getByTestId("workflow-timeline-duration")).toHaveTextContent("3.00 s");
    expect(screen.queryByTestId("workflow-right-column")).not.toBeInTheDocument();
  });

  it("rejects from the overlay", async () => {
    await runView();
    waitForApproval();
    fireEvent.click(within(screen.getByTestId("workflow-approval-overlay")).getByRole("button", { name: "Reject" }));
    // Rejecting fails the run, so it asks first; nothing is sent until the dialog is confirmed.
    const dialog = await screen.findByRole("dialog", { name: "Reject step approve?" });
    expect(posts).toEqual([]);
    fireEvent.click(within(dialog).getByRole("button", { name: "Reject" }));
    await waitFor(() => expect(posts.map((p) => p.path)).toEqual(["/api/v1/workflows/runs/run-1/steps/approve/reject"]));
  });

  it("stays reachable with the panel collapsed and at phone width (outside the bottom Sheet)", async () => {
    await runView();
    fireEvent.click(screen.getByTestId("workflow-panel-toggle"));
    waitForApproval();
    expect(within(screen.getByTestId("workflow-approval-overlay")).getByRole("button", { name: "Approve" })).toBeEnabled();
    cleanup();
    restoreWidth = setWindowWidth(375);
    await runView();
    waitForApproval();
    expect(screen.queryByTestId("workflow-panel-sheet")).not.toBeInTheDocument();
    expect(within(screen.getByTestId("workflow-approval-overlay")).getByRole("button", { name: "Approve" })).toBeEnabled();
  });
});

describe("step detail: right column or Sheet", () => {
  it("is a floating column where the fit rule leaves room", async () => {
    restoreWidth = setWindowWidth(1440);
    await runView();
    fireEvent.click(nodeButton("classify"));
    expect(within(screen.getByTestId("workflow-right-column")).getByTestId("step-io-panel")).toHaveAttribute("data-step-id", "classify");
    expect(screen.queryByTestId("workflow-step-sheet")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Close step details" }));
    expect(screen.queryByTestId("step-io-panel")).not.toBeInTheDocument();
  });

  it("is a Sheet at 800 px, and the Sheet's content is the same panel", async () => {
    restoreWidth = setWindowWidth(800);
    await runView();
    fireEvent.click(nodeButton("classify"));
    const sheet = await screen.findByTestId("workflow-step-sheet");
    expect(sheet).toHaveAccessibleName("Step classify");
    expect(within(sheet).getByTestId("step-io-panel")).toHaveAttribute("data-step-id", "classify");
    expect(screen.queryByTestId("workflow-right-column")).not.toBeInTheDocument();
    fireEvent.keyDown(sheet, { key: "Escape" });
    await waitFor(() => expect(screen.queryByTestId("workflow-step-sheet")).not.toBeInTheDocument());
  });

  it("collapsing the left panel gives the column room again at 800 px; the approval stays in the column beside it", async () => {
    restoreWidth = setWindowWidth(800);
    await runView();
    fireEvent.click(screen.getByTestId("workflow-panel-toggle"));
    // The step list is in the collapsed panel: pick the step on the canvas.
    fireEvent.click(screen.getByTestId("workflow-node-classify"));
    expect(within(screen.getByTestId("workflow-right-column")).getByTestId("step-io-panel")).toBeInTheDocument();
    expect(screen.queryByTestId("workflow-step-sheet")).not.toBeInTheDocument();
    waitForApproval();
    const column = screen.getByTestId("workflow-right-column");
    expect(within(column).getByTestId("workflow-approval-overlay")).toBeInTheDocument();
    expect(within(column).getByTestId("step-io-panel")).toBeInTheDocument();
  });

  it("in the frame, a wider panel pushes the detail into a Sheet: 1280 px floats at 380 and sheets at 560", async () => {
    media = mockMatchMedia(true);
    restoreWidth = setWindowWidth(1280);
    go(routes.workflowRun("wf-1", "run-1"), { id: "wf-1", runId: "run-1" });
    render(<FrameHarness><WorkflowRunPage /></FrameHarness>);
    await screen.findByTestId("workflow-node-classify");
    fireEvent.click(nodeButton("classify"));
    expect(within(screen.getByTestId("workflow-right-column")).getByTestId("step-io-panel")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Close step details" }));
    fireEvent.keyDown(resize(), { key: "End" });
    fireEvent.click(nodeButton("classify"));
    expect(await screen.findByTestId("workflow-step-sheet")).toBeInTheDocument();
  });
});

const url = () => `${nav.path}${nav.search ? `?${nav.search}` : ""}`;
const params = () => Object.fromEntries(new URLSearchParams(nav.search));
const traceSheet = () => screen.findByTestId("trace-sheet");

describe("trace Sheets in place", () => {
  it("Run details opens the Events tab in a Sheet on the run page; Back closes it", async () => {
    await runView();
    const link = screen.getByRole("link", { name: "Run details" });
    link.focus();
    fireEvent.click(link);
    const sheet = await traceSheet();
    expect(nav.path).toBe(routes.workflowRun("wf-1", "run-1"));
    expect(params()).toEqual({ trace: "run-1", traceTab: "events" });
    expect(within(sheet).getByTestId("trace-sheet-run-id")).toHaveTextContent("Run run-1");
    expect(within(sheet).getByRole("tab", { name: /Events/ })).toHaveAttribute("aria-selected", "true");
    expect(screen.getByTestId("workflow-run-view")).toBeInTheDocument();
    expect(nav.push).toHaveBeenCalledTimes(1);

    act(() => nav.back());
    await waitFor(() => expect(screen.queryByTestId("trace-sheet")).not.toBeInTheDocument());
    expect(url()).toBe(routes.workflowRun("wf-1", "run-1"));
    await waitFor(() => expect(link).toHaveFocus());
  });

  it("the Sheet's Escape goes Back too, and the page is still the run view", async () => {
    await runView();
    fireEvent.click(screen.getByRole("link", { name: "Run details" }));
    fireEvent.keyDown(await traceSheet(), { key: "Escape" });
    await waitFor(() => expect(screen.queryByTestId("trace-sheet")).not.toBeInTheDocument());
    expect(nav.back).toHaveBeenCalledTimes(1);
    expect(screen.getByTestId("workflow-run-view")).toBeInTheDocument();
  });

  it("a step's child-run link opens that run's trace Sheet in place", async () => {
    await runView();
    emit(ev("step_started", { step_id: "classify", step_type: "agent", child_run_id: "child-9-abcdefgh" }, 0));
    fireEvent.click(screen.getByRole("button", { name: "Step classify, Agent, Running" }));
    const link = screen.getByTestId("step-child-run-link");
    expect(link).toHaveAttribute("href", routes.trace("child-9-abcdefgh", { tab: "events" }));
    fireEvent.click(link);
    const sheet = await traceSheet();
    expect(params()).toEqual({ trace: "child-9-abcdefgh", traceTab: "events" });
    expect(within(sheet).getByTestId("trace-sheet-run-id")).toHaveTextContent("child-9-abcdefgh");
    expect(nav.path).toBe(routes.workflowRun("wf-1", "run-1"));
    // The step stays selected beneath the Sheet.
    expect(screen.getByTestId("step-io-panel")).toHaveAttribute("data-step-id", "classify");
    act(() => nav.back());
    await waitFor(() => expect(screen.queryByTestId("trace-sheet")).not.toBeInTheDocument());
  });

  it("a modified click on Run details follows the link instead of opening the Sheet", async () => {
    await runView();
    fireEvent.click(screen.getByRole("link", { name: "Run details" }), { ctrlKey: true });
    expect(nav.push).not.toHaveBeenCalled();
    expect(screen.queryByTestId("trace-sheet")).not.toBeInTheDocument();
  });

  it("the Traces tab opens a row's trace in place, switches its tab on traceTab and Back closes", async () => {
    go(routes.workflowTraces("wf-1"));
    render(<WorkflowTracesPage />);
    const list = await screen.findByTestId("workflow-traces-list");
    await waitFor(() => expect(within(list).getAllByTestId("workflow-trace-open")).toHaveLength(2));
    fireEvent.click(within(list).getAllByTestId("workflow-trace-open")[0]);
    const sheet = await traceSheet();
    expect(url()).toBe(`${routes.workflowTraces("wf-1")}?trace=run-b`);
    expect(within(sheet).getByTestId("trace-sheet-position")).toHaveTextContent("1");
    fireEvent.mouseDown(within(sheet).getByRole("tab", { name: /Events/ }));
    fireEvent.click(within(sheet).getByRole("tab", { name: /Events/ }));
    await waitFor(() => expect(params()).toEqual({ trace: "run-b", traceTab: "events" }));
    expect(nav.path).toBe(routes.workflowTraces("wf-1"));
    expect(screen.getByTestId("workflow-tab-traces")).toHaveAttribute("aria-current", "page");
    act(() => nav.back());
    await waitFor(() => expect(screen.queryByTestId("trace-sheet")).not.toBeInTheDocument());
    expect(url()).toBe(routes.workflowTraces("wf-1"));
  });

  it("?traceTab does not clash with ?tab=runs: both live in one URL and each keeps its meaning", async () => {
    go(`${routes.workflow("wf-1", { tab: "runs" })}&trace=run-a&traceTab=events`);
    render(<WorkflowDetailPage />);
    const sheet = await traceSheet();
    expect(within(sheet).getByTestId("trace-sheet-run-id")).toHaveTextContent("Run run-a");
    expect(within(sheet).getByRole("tab", { name: /Events/ })).toHaveAttribute("aria-selected", "true");
    expect(screen.getByTestId("workflow-tab-runs")).toHaveAttribute("aria-current", "page");
    expect(await screen.findByTestId("workflow-run-history")).toBeInTheDocument();
    // Closing a Sheet that came with the URL replaces it and leaves the page's own tab alone.
    fireEvent.keyDown(sheet, { key: "Escape" });
    await waitFor(() => expect(screen.queryByTestId("trace-sheet")).not.toBeInTheDocument());
    expect(params()).toEqual({ tab: "runs" });
    expect(screen.getByTestId("workflow-tab-runs")).toHaveAttribute("aria-current", "page");
  });

  it("a Sheet in the URL with the default Graph tab leaves the Graph, with its panel, in place", async () => {
    go(`${routes.workflow("wf-1")}?trace=run-a`);
    render(<WorkflowDetailPage />);
    await traceSheet();
    expect(screen.getByTestId("workflow-tab-graph")).toHaveAttribute("aria-current", "page");
    expect(screen.getByTestId("workflow-graph")).toBeInTheDocument();
  });
});

describe("zoom controls", () => {
  const NAMES = [["workflow-zoom-out", "Zoom out"], ["workflow-zoom-reset", "Zoom to 100%"], ["workflow-zoom-in", "Zoom in"], ["workflow-zoom-fit", "Fit graph to view"]] as const;

  it.each([["Graph tab", "graph"], ["run view", "run"]])("on the %s: four named native buttons in a Zoom group, in tab order, outside the graph image", async (_n, page) => {
    if (page === "graph") await graphTab();
    else await runView();
    const group = screen.getByRole("group", { name: "Zoom" });
    expect(screen.getByTestId("workflow-canvas")).toContainElement(group);
    expect(screen.getByTestId("workflow-graph")).not.toContainElement(group);
    const buttons = within(group).getAllByRole("button");
    expect(buttons.map((b) => b.getAttribute("data-testid"))).toEqual(NAMES.map(([id]) => id));
    for (const [id, name] of NAMES) {
      const button = screen.getByTestId(id);
      expect(button.tagName).toBe("BUTTON");
      expect(button).toHaveAccessibleName(name);
      expect(button).toBeEnabled();
      expect(button.tabIndex).toBeGreaterThanOrEqual(0);
      button.focus();
      expect(button).toHaveFocus();
      fireEvent.click(button);
      expect(button).toBeInTheDocument();
    }
  });
});

function sourceFiles(dir: string): string[] {
  return fs.readdirSync(dir, { withFileTypes: true }).flatMap((entry) => {
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) return sourceFiles(full);
    return /\.(ts|tsx)$/.test(entry.name) && !/\.test\.tsx?$/.test(entry.name) ? [full] : [];
  });
}

describe("no hard-coded colours in the workflow code", () => {
  const roots = [path.join(__dirname, "..", "..", "components", "workflows"), __dirname];
  const PALETTE = "white|black|slate|gray|grey|zinc|neutral|stone|red|orange|amber|yellow|lime|green|emerald|teal|cyan|sky|blue|indigo|violet|purple|fuchsia|pink|rose";
  const patterns: Array<[string, RegExp]> = [
    ["hex colour", /["'`(\s:,]#[0-9a-fA-F]{3,8}\b(?![-\w])/],
    // hsl(var(--token)) is the theme: the token holds the triplet. A literal triplet is a hard-coded colour.
    ["rgb/hsl function", /\b(?:rgba?|hsla?|oklch|oklab|lab|lch)\((?!\s*var\(--)/],
    ["palette utility", new RegExp(`\\b(?:bg|text|border|ring|fill|stroke|from|to|via|divide|outline|decoration|shadow|accent|caret)-(?:${PALETTE})(?:-\\d{2,3})?(?![-\\w])`)],
    ["arbitrary colour value", /\[(?:#|rgb|hsl)/],
  ];

  it("scans real files", () => {
    const files = roots.flatMap(sourceFiles);
    expect(files.some((f) => f.endsWith("WorkflowFloatingPanel.tsx"))).toBe(true);
    expect(files.some((f) => f.endsWith("TimelineDock.tsx"))).toBe(true);
    expect(files.some((f) => f.endsWith(path.join("[id]", "traces", "page.tsx")))).toBe(true);
    // 14 since AC.14 moved ApprovalCard to components/approvals/.
    expect(files.length).toBeGreaterThanOrEqual(14);
  });

  it("finds no hex, rgb/hsl, Tailwind palette or arbitrary colour in components/workflows or app/workflows", () => {
    const hits: string[] = [];
    for (const file of roots.flatMap(sourceFiles)) {
      fs.readFileSync(file, "utf-8").split("\n").forEach((line, index) => {
        for (const [label, pattern] of patterns) if (pattern.test(line)) hits.push(`${path.relative(__dirname, file)}:${index + 1} ${label}: ${line.trim()}`);
      });
    }
    expect(hits).toEqual([]);
  });
});

describe.each(["light", "dark"] as const)("axe and one h1 per page, inside the frame (%s theme)", (theme) => {
  beforeEach(() => {
    media = mockMatchMedia(true);
    restoreWidth = setWindowWidth(1440);
    localStorage.setItem("studio.theme", theme);
    document.documentElement.classList.toggle("dark", theme === "dark");
  });
  afterEach(() => localStorage.removeItem("studio.theme"));

  async function clean(h1: string) {
    expect(document.documentElement.classList.contains("dark")).toBe(theme === "dark");
    expect(screen.getAllByRole("heading", { level: 1 })).toHaveLength(1);
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent(h1);
    await expectNoAxeViolations(document.body);
  }

  it("the list", async () => {
    go(routes.workflows(), {});
    render(<FrameHarness><WorkflowsPage /></FrameHarness>);
    await screen.findByTestId("workflow-row-wf-1");
    await clean("Workflows");
  }, AXE_TIMEOUT_MS);

  it("the Graph tab, with the panel open", async () => {
    render(<FrameHarness><WorkflowDetailPage /></FrameHarness>);
    await screen.findByTestId("workflow-run-row-run-b");
    await clean("Triage");
  });

  it("the Graph tab, with the panel collapsed to its rail", async () => {
    localStorage.setItem(WORKFLOW_LAYOUT_STORAGE_KEY, JSON.stringify({ panelCollapsed: true }));
    render(<FrameHarness><WorkflowDetailPage /></FrameHarness>);
    await screen.findByTestId("workflow-graph");
    await waitFor(() => expect(screen.getByTestId("workflow-panel")).toHaveAttribute("data-collapsed", "true"));
    await clean("Triage");
  });

  it("the run view with the approval overlay, a step open and the Timeline expanded", async () => {
    localStorage.setItem(WORKFLOW_LAYOUT_STORAGE_KEY, JSON.stringify({ timelineOpen: true }));
    go(routes.workflowRun("wf-1", "run-1"), { id: "wf-1", runId: "run-1" });
    render(<FrameHarness><WorkflowRunPage /></FrameHarness>);
    await screen.findByTestId("workflow-step-classify");
    emit(ev("step_started", { step_id: "classify", step_type: "agent" }, 0), ev("step_completed", { step_id: "classify", output: "x" }, 1));
    waitForApproval();
    fireEvent.click(screen.getByRole("button", { name: "Step classify, Agent, Completed" }));
    expect(screen.getByTestId("workflow-approval-overlay")).toBeInTheDocument();
    expect(screen.getByTestId("step-io-panel")).toBeInTheDocument();
    expect(await screen.findByTestId("workflow-timeline")).toBeInTheDocument();
    await clean("Run run-1");
  });

  it("the Traces tab, then with a trace Sheet open", async () => {
    go(routes.workflowTraces("wf-1"));
    render(<FrameHarness><WorkflowTracesPage /></FrameHarness>);
    const list = await screen.findByTestId("workflow-traces-list");
    await waitFor(() => expect(within(list).getAllByTestId("workflow-trace-open")).toHaveLength(2));
    await clean("Triage");
    fireEvent.click(within(list).getAllByTestId("workflow-trace-open")[0]);
    await traceSheet();
    await expectNoAxeViolations(document.body);
  }, AXE_TIMEOUT_MS);

  it("the phone Sheet over the Graph tab", async () => {
    restoreWidth();
    restoreWidth = setWindowWidth(375);
    render(<FrameHarness><WorkflowDetailPage /></FrameHarness>);
    await screen.findByTestId("workflow-graph");
    fireEvent.click(await screen.findByTestId("workflow-panel-open"));
    await screen.findByTestId("workflow-panel-sheet");
    await expectNoAxeViolations(document.body);
  }, AXE_TIMEOUT_MS);
});
