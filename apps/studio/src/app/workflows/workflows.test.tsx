import React from "react";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import { demoSpec } from "@/test/workflowFixtures";
import { routes } from "@/lib/routes";

const push = vi.fn();
// SL.14: the detail page reads its tab (`?tab=runs`) and hosts the trace Sheet, so it also needs
// the search parameters, the pathname and router.replace.
vi.mock("next/navigation", () => ({
  useParams: () => ({ id: "wf-1" }),
  useRouter: () => ({ push, replace: vi.fn() }),
  useSearchParams: () => new URLSearchParams(),
  usePathname: () => "/workflows/wf-1",
}));
vi.mock("next/link", () => ({ default: ({ href, children, ...rest }: { href: string; children: React.ReactNode }) => <a href={href} {...rest}>{children}</a> }));

import WorkflowsPage from "./page";
import WorkflowDetailPage from "./[id]/page";
import { workflowRunLedger } from "@/lib/workflows";

const fetchMock = vi.fn();
const json = (status: number, body: unknown) => Promise.resolve(new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } }));

const WF = { id: "wf-1", name: "Triage", draft_spec: demoSpec, latest_version: 2 };
const V2 = { workflow_id: "wf-1", version: 2, spec: demoSpec, spec_hash: "h2", created_at: "2026-11-13T09:00:00Z", created_by: "dev" };
const V1 = { ...V2, version: 1, spec: { ...demoSpec, steps: demoSpec.steps.slice(0, 1).map((s) => ({ ...s, next: null })) }, spec_hash: "h1" };

let runResponses: Array<() => Promise<Response>> = [];

function route(url: string, init?: RequestInit): Promise<Response> {
  const u = new URL(url);
  if (u.pathname === "/api/v1/workflows") return json(200, [WF, { id: "wf-2", name: "Draft", draft_spec: demoSpec, latest_version: null }]);
  if (u.pathname === "/api/v1/workflows/wf-1") return json(200, WF);
  if (u.pathname === "/api/v1/workflows/wf-1/versions") return json(200, [V2, V1]);
  if (u.pathname === "/api/v1/workflows/wf-1/versions/2") return json(200, V2);
  if (u.pathname === "/api/v1/runs") return json(200, { items: [{ run_id: "aaaaaaaa-1", kind: "workflow", workflow_id: "wf-1", workflow_version: 2, display_status: "completed", status: "completed", created_at: "2026-11-13T10:00:00Z", updated_at: "2026-11-13T10:00:02.500Z" }], next_before: null });
  if (u.pathname === "/api/v1/workflows/wf-1/runs" && init?.method === "POST") return runResponses.shift()!();
  return json(404, { detail: "not mocked" });
}

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
  push.mockReset();
  workflowRunLedger.reset();
  runResponses = [];
});

afterEach(() => {
  cleanup();
  vi.unstubAllEnvs();
});

describe("/workflows", () => {
  it("lists workflows with latest version, updated and a link", async () => {
    render(<WorkflowsPage />);
    const row = await screen.findByTestId("workflow-row-wf-1");
    expect(within(row).getByRole("link", { name: "Triage" })).toHaveAttribute("href", routes.workflow("wf-1"));
    expect(row).toHaveTextContent("v2");
    await waitFor(() => expect(row).toHaveTextContent("2026"));
    expect(screen.getByTestId("workflow-row-wf-2")).toHaveTextContent("Draft only");
  });
});

describe("/workflows/[id]", () => {
  async function renderDetail() {
    render(<WorkflowDetailPage />);
    await screen.findByTestId("workflow-graph");
  }

  it("renders the published graph, switches versions and shows run history", async () => {
    await renderDetail();
    expect(screen.getByTestId("workflow-step-route")).toHaveTextContent("1: input.urgent eq true");
    expect(screen.getByTestId("workflow-node-route")).toHaveAttribute("data-step-type", "branch");
    fireEvent.change(screen.getByLabelText("Version"), { target: { value: "1" } });
    await waitFor(() => expect(screen.queryByTestId("workflow-step-route")).toBeNull());
    const row = await screen.findByTestId("workflow-run-row-aaaaaaaa-1");
    expect(within(row).getByRole("link")).toHaveAttribute("href", routes.workflowRun("wf-1", "aaaaaaaa-1"));
    expect(row).toHaveTextContent("2.50 s");
  });

  it("maps 422 field errors onto the form", async () => {
    await renderDetail();
    runResponses.push(() => json(422, { detail: [{ loc: ["body", "input", "text"], msg: "does not satisfy the input schema's 'required' keyword", type: "json_schema.required" }] }));
    fireEvent.click(screen.getByRole("button", { name: "Run workflow" }));
    expect(await screen.findByTestId("field-error-text")).toHaveTextContent("required");
    expect(push).not.toHaveBeenCalled();
  });

  it("reuses the run id after a lost response, never retries by itself, and opens the run once accepted", async () => {
    await renderDetail();
    fireEvent.change(screen.getByLabelText("Text *"), { target: { value: "hello" } });
    runResponses.push(() => Promise.reject(new TypeError("Failed to fetch")));
    fireEvent.click(screen.getByRole("button", { name: "Run workflow" }));
    expect(await screen.findByTestId("workflow-run-banner")).toHaveTextContent("reuse the same run id");
    expect(screen.getByTestId("workflow-run-reuse-hint")).toHaveAttribute("data-state", "uncertain");
    const posts = () => fetchMock.mock.calls.filter(([, init]) => init?.method === "POST");
    expect(posts()).toHaveLength(1);
    const firstId = JSON.parse(posts()[0][1].body).run_id;

    runResponses.push(() => json(202, { run_id: firstId, kind: "workflow", workflow_id: "wf-1", version: 2, status: "queued", display_status: "queued", trace_id: null }));
    fireEvent.click(screen.getByRole("button", { name: "Run workflow" }));
    await waitFor(() => expect(push).toHaveBeenCalledWith(routes.workflowRun("wf-1", firstId)));
    const body = JSON.parse(posts()[1][1].body);
    expect(body).toEqual({ input: { text: "hello" }, version: 2, run_id: firstId });
  });
});
