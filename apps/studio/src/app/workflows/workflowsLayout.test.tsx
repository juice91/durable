import React from "react";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import { demoSpec } from "@/test/workflowFixtures";
import { expectNoAxeViolations, AXE_TIMEOUT_MS } from "@/test/a11y";
import { setWindowWidth } from "@/components/frame/frameTesting";
import { routes } from "@/lib/routes";
import { workflowRunLedger } from "@/lib/workflows";
import { PANEL_DEFAULT_WIDTH, PANEL_KEY_STEP, WORKFLOW_LAYOUT_STORAGE_KEY } from "@/components/workflows/workflowLayout";

const nav = vi.hoisted(() => ({
  push: vi.fn(),
  replace: vi.fn(),
  search: new URLSearchParams(),
  pathname: "/workflows/wf-1",
}));
vi.mock("next/navigation", () => ({
  useParams: () => ({ id: "wf-1" }),
  useRouter: () => ({ push: nav.push, replace: nav.replace }),
  useSearchParams: () => nav.search,
  usePathname: () => nav.pathname,
}));
vi.mock("next/link", () => ({ default: ({ href, children, ...rest }: { href: string; children: React.ReactNode }) => <a href={href} {...rest}>{children}</a> }));

import WorkflowDetailPage from "./[id]/page";
import WorkflowTracesPage from "./[id]/traces/page";

const fetchMock = vi.fn();
const json = (status: number, body: unknown) => Promise.resolve(new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } }));
const WF = { id: "wf-1", name: "Triage", draft_spec: demoSpec, latest_version: 2 };
const V2 = { workflow_id: "wf-1", version: 2, spec: demoSpec, spec_hash: "h2", created_at: "2026-11-13T09:00:00Z", created_by: "dev" };
const RUN = (id: string, minute: number) => ({
  run_id: id, kind: "workflow", workflow_id: "wf-1", workflow_version: 2, display_status: "completed", status: "completed",
  created_at: `2026-11-13T10:0${minute}:00Z`, updated_at: `2026-11-13T10:0${minute}:02Z`, input_tokens: 10, output_tokens: 5, cost_usd: null,
});

/** The next POST /workflows/wf-1/runs answer; a test can hold it to keep the submit in flight. */
let startRun: () => Promise<Response> = () => json(202, { run_id: "run-new", kind: "workflow", workflow_id: "wf-1", version: 2, status: "queued", display_status: "queued", trace_id: null });

function route(url: string, init?: RequestInit): Promise<Response> {
  const u = new URL(url);
  if (u.pathname === "/api/v1/workflows/wf-1/runs" && init?.method === "POST") return startRun();
  if (u.pathname === "/api/v1/workflows") return json(200, [WF, { id: "wf-2", name: "Other", draft_spec: demoSpec, latest_version: null }]);
  if (u.pathname === "/api/v1/workflows/wf-1") return json(200, WF);
  if (u.pathname === "/api/v1/workflows/wf-1/versions") return json(200, [V2]);
  if (u.pathname === "/api/v1/runs") return json(200, { items: [RUN("run-b", 2), RUN("run-a", 1)], next_before: null });
  return json(404, { detail: "not mocked" });
}

let restoreWidth: () => void = () => undefined;

beforeAll(() => {
  class RO { observe() {} unobserve() {} disconnect() {} }
  vi.stubGlobal("ResizeObserver", RO);
  vi.stubGlobal("DOMMatrixReadOnly", class { m22 = 1; });
});

beforeEach(() => {
  vi.stubEnv("NEXT_PUBLIC_EAP_DEV_TOKEN", "Bearer test");
  fetchMock.mockReset();
  fetchMock.mockImplementation(route);
  vi.stubGlobal("fetch", fetchMock);
  nav.push.mockReset();
  nav.replace.mockReset();
  nav.search = new URLSearchParams();
  nav.pathname = "/workflows/wf-1";
  workflowRunLedger.reset();
  localStorage.clear();
  startRun = () => json(202, { run_id: "run-new", kind: "workflow", workflow_id: "wf-1", version: 2, status: "queued", display_status: "queued", trace_id: null });
});

afterEach(() => {
  cleanup();
  restoreWidth();
  restoreWidth = () => undefined;
  vi.unstubAllEnvs();
});

async function renderGraphTab() {
  render(<WorkflowDetailPage />);
  await screen.findByTestId("workflow-graph");
  await screen.findByTestId("workflow-run-row-run-b");
}

describe("workflow detail: Graph tab layout (SL.14)", () => {
  it("shows the tabs, a full-bleed canvas and the floating panel with Run, Recent runs and Steps", async () => {
    await renderGraphTab();
    const tabs = screen.getByRole("navigation", { name: "Workflow sections" });
    expect(within(tabs).getByRole("link", { name: "Graph" })).toHaveAttribute("aria-current", "page");
    expect(within(tabs).getByRole("link", { name: "Runs" })).toHaveAttribute("href", routes.workflow("wf-1", { tab: "runs" }));
    expect(within(tabs).getByRole("link", { name: "Traces" })).toHaveAttribute("href", routes.workflowTraces("wf-1"));
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent("Triage");

    const panel = screen.getByRole("region", { name: "Workflow" });
    expect(panel).toHaveStyle({ width: `${PANEL_DEFAULT_WIDTH}px` });
    expect(within(panel).getByTestId("workflow-run-panel")).toBeInTheDocument();
    expect(within(panel).getByRole("heading", { name: "Recent runs" })).toBeInTheDocument();
    expect(within(panel).getByTestId("workflow-all-runs")).toHaveAttribute("href", routes.workflow("wf-1", { tab: "runs" }));
    expect(within(panel).getByTestId("workflow-step-list")).toBeInTheDocument();
  });

  it("has labelled zoom controls outside the graph image", async () => {
    await renderGraphTab();
    const zoom = screen.getByRole("group", { name: "Zoom" });
    for (const name of ["Zoom in", "Zoom out", "Zoom to 100%", "Fit graph to view"]) {
      expect(within(zoom).getByRole("button", { name })).toBeInTheDocument();
    }
    expect(screen.getByTestId("workflow-graph")).not.toContainElement(zoom);
    fireEvent.click(within(zoom).getByRole("button", { name: "Zoom in" }));
    fireEvent.click(within(zoom).getByRole("button", { name: "Fit graph to view" }));
  });

  it("collapses the panel and keeps it collapsed after a reload", async () => {
    await renderGraphTab();
    const hide = screen.getByRole("button", { name: "Hide workflow panel" });
    expect(hide).toHaveAttribute("aria-expanded", "true");
    fireEvent.click(hide);
    expect(screen.queryByTestId("workflow-run-panel")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Show workflow panel" })).toHaveAttribute("aria-expanded", "false");
    expect(JSON.parse(localStorage.getItem(WORKFLOW_LAYOUT_STORAGE_KEY)!)).toMatchObject({ panelCollapsed: true });

    cleanup();
    render(<WorkflowDetailPage />);
    await screen.findByTestId("workflow-graph");
    expect(await screen.findByRole("button", { name: "Show workflow panel" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Show workflow panel" }));
    expect(await screen.findByTestId("workflow-run-panel")).toBeInTheDocument();
  });

  it("resizes the panel from the keyboard within the fit rule and keeps the width after a reload", async () => {
    await renderGraphTab();
    const handle = screen.getByRole("separator", { name: "Resize workflow panel" });
    expect(handle).toHaveAttribute("aria-valuenow", String(PANEL_DEFAULT_WIDTH));
    fireEvent.keyDown(handle, { key: "ArrowRight" });
    expect(handle).toHaveAttribute("aria-valuenow", String(PANEL_DEFAULT_WIDTH + PANEL_KEY_STEP));
    fireEvent.keyDown(handle, { key: "End" });
    const max = Number(handle.getAttribute("aria-valuemax"));
    expect(handle).toHaveAttribute("aria-valuenow", String(max));
    fireEvent.keyDown(handle, { key: "ArrowRight" });
    expect(handle).toHaveAttribute("aria-valuenow", String(max));
    expect(JSON.parse(localStorage.getItem(WORKFLOW_LAYOUT_STORAGE_KEY)!)).toMatchObject({ panelWidth: max });

    cleanup();
    render(<WorkflowDetailPage />);
    await screen.findByTestId("workflow-graph");
    await waitFor(() => expect(screen.getByRole("separator", { name: "Resize workflow panel" })).toHaveAttribute("aria-valuenow", String(max)));
  });

  it("is a bottom Sheet below 768 px, opened from the canvas", async () => {
    restoreWidth = setWindowWidth(375);
    render(<WorkflowDetailPage />);
    await screen.findByTestId("workflow-graph");
    expect(screen.queryByTestId("workflow-run-panel")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Show workflow" }));
    const sheet = await screen.findByRole("dialog", { name: "Workflow" });
    expect(within(sheet).getByTestId("workflow-run-panel")).toBeInTheDocument();
    expect(within(sheet).getByRole("button", { name: "Run workflow" })).toBeInTheDocument();
  });

  it("shows the whole run history on the Runs tab", async () => {
    nav.search = new URLSearchParams("tab=runs");
    render(<WorkflowDetailPage />);
    const history = await screen.findByTestId("workflow-run-history");
    expect(within(history).getByRole("heading", { name: "Run history" })).toBeInTheDocument();
    expect(await screen.findByTestId("workflow-run-row-run-a")).toBeInTheDocument();
    expect(screen.queryByTestId("workflow-graph")).not.toBeInTheDocument();
    expect(within(screen.getByRole("navigation", { name: "Workflow sections" })).getByRole("link", { name: "Runs" })).toHaveAttribute("aria-current", "page");
  });

  it("has no axe violations on the Graph tab", async () => {
    await renderGraphTab();
    // The page (the app layout sets <html lang>; this test renders the page alone).
    await expectNoAxeViolations(document.body);
  }, AXE_TIMEOUT_MS);
});

describe("the panel keeps its content mounted (SL.14 review)", () => {
  it("typed Run input survives collapsing and expanding the panel", async () => {
    await renderGraphTab();
    fireEvent.change(screen.getByLabelText("Text *"), { target: { value: "keep me" } });
    fireEvent.click(screen.getByRole("button", { name: "Hide workflow panel" }));
    expect(screen.queryByLabelText("Text *")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Show workflow panel" }));
    expect(screen.getByLabelText("Text *")).toHaveValue("keep me");
  });

  it("a submit in flight while the panel is collapsed still opens the run page", async () => {
    let answer: (response: Response) => void = () => undefined;
    startRun = () => new Promise<Response>((resolve) => { answer = resolve; });
    await renderGraphTab();
    fireEvent.change(screen.getByLabelText("Text *"), { target: { value: "go" } });
    fireEvent.click(screen.getByRole("button", { name: "Run workflow" }));
    fireEvent.click(screen.getByRole("button", { name: "Hide workflow panel" }));
    answer(new Response(JSON.stringify({ run_id: "run-new", kind: "workflow", workflow_id: "wf-1", version: 2, status: "queued", display_status: "queued", trace_id: null }), { status: 202, headers: { "Content-Type": "application/json" } }));
    await waitFor(() => expect(nav.push).toHaveBeenCalledWith(routes.workflowRun("wf-1", "run-new")));
  });

  it("typed input survives the window crossing 768 px (floating panel to bottom Sheet)", async () => {
    await renderGraphTab();
    fireEvent.change(screen.getByLabelText("Text *"), { target: { value: "resize me" } });
    restoreWidth = setWindowWidth(375);
    act(() => { window.dispatchEvent(new Event("resize")); });
    fireEvent.click(await screen.findByTestId("workflow-panel-open"));
    const sheet = await screen.findByTestId("workflow-panel-sheet");
    expect(within(sheet).getByLabelText("Text *")).toHaveValue("resize me");
  });
});

describe("workflow Traces tab (SL.14)", () => {
  it("lists the workflow's runs and opens a trace Sheet in place on traceTab", async () => {
    nav.pathname = routes.workflowTraces("wf-1");
    render(<WorkflowTracesPage />);
    const list = await screen.findByTestId("workflow-traces-list");
    await waitFor(() => expect(within(list).getAllByTestId("workflow-trace-row")).toHaveLength(2));
    expect(within(screen.getByRole("navigation", { name: "Workflow sections" })).getByRole("link", { name: "Traces" })).toHaveAttribute("aria-current", "page");
    // The list request asks for this workflow's runs.
    const runsCall = fetchMock.mock.calls.map(([url]) => new URL(String(url))).find((u) => u.pathname === "/api/v1/runs");
    expect(runsCall?.searchParams.get("workflow_id")).toBe("wf-1");
    expect(runsCall?.searchParams.get("kind")).toBe("workflow");

    fireEvent.click(within(list).getAllByTestId("workflow-trace-open")[0]);
    expect(nav.push).toHaveBeenCalledWith(`${routes.workflowTraces("wf-1")}?trace=run-b`, { scroll: false });
  });
});
