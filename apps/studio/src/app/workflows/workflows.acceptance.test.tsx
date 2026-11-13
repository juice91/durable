import React from "react";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import { buildWorkflowGraph } from "@/lib/workflowGraph";
import { canonicalInputKey } from "@/lib/toolRunLedger";
import { workflowRunLedger, type WorkflowSpec } from "@/lib/workflows";
import { demoSpec, parallelSpec } from "@/test/workflowFixtures";

const push = vi.fn();
vi.mock("next/navigation", () => ({ useParams: () => ({ id: "wf-1" }), useRouter: () => ({ push }) }));
vi.mock("next/link", () => ({
  default: ({ href, children, ...rest }: { href: string; children: React.ReactNode }) => <a href={href} {...rest}>{children}</a>,
}));

import WorkflowsPage from "./page";
import WorkflowDetailPage from "./[id]/page";

const fetchMock = vi.fn();

/** Input schema with a nested field so a 422 list can land on more than one control. */
const inputSchema: WorkflowSpec["input_schema"] = {
  type: "object",
  properties: {
    text: { type: "string", title: "Text" },
    urgent: { type: "boolean", title: "Urgent" },
    options: {
      type: "object",
      title: "Options",
      properties: { retries: { type: "integer", title: "Retries" } },
    },
  },
  required: ["text"],
};

const publishedSpec: WorkflowSpec = { ...demoSpec, input_schema: inputSchema };
let spec: WorkflowSpec = publishedSpec;

const WF = { id: "wf-1", name: "Triage", draft_spec: publishedSpec, latest_version: 2 };
const DRAFT = { id: "wf-2", name: "Draft", draft_spec: publishedSpec, latest_version: null };

function json(status: number, body: unknown) {
  return Promise.resolve(new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } }));
}

function versionBody(version: number) {
  return {
    workflow_id: "wf-1",
    version,
    spec,
    spec_hash: `h${version}`,
    created_at: "2026-11-13T09:00:00Z",
    created_by: "dev",
  };
}

type RunResponse = (init?: RequestInit) => Promise<Response>;
let runResponses: RunResponse[] = [];

function accepted(runId: string, version: number) {
  return { run_id: runId, kind: "workflow", workflow_id: "wf-1", version, status: "queued", display_status: "queued", trace_id: null };
}

function route(url: string, init?: RequestInit): Promise<Response> {
  const u = new URL(url);
  if (u.pathname === "/api/v1/workflows") return json(200, [WF, DRAFT]);
  if (u.pathname === "/api/v1/workflows/wf-1") return json(200, WF);
  if (u.pathname === "/api/v1/workflows/wf-1/versions") return json(200, [versionBody(2), versionBody(1)]);
  if (u.pathname === "/api/v1/workflows/wf-1/versions/2") return json(200, versionBody(2));
  if (u.pathname === "/api/v1/workflows/wf-1/versions/1") return json(200, versionBody(1));
  if (u.pathname === "/api/v1/runs") return json(200, { items: [], next_before: null });
  if (u.pathname === "/api/v1/workflows/wf-1/runs" && init?.method === "POST") {
    const next = runResponses.shift();
    if (!next) return json(500, { detail: "no queued response" });
    return next(init);
  }
  return json(404, { detail: "not mocked" });
}

function postBodies(): Array<{ input: Record<string, unknown>; version?: number; run_id?: string }> {
  return fetchMock.mock.calls
    .filter(([, init]) => init?.method === "POST")
    .map(([, init]) => JSON.parse(String(init.body)) as { input: Record<string, unknown>; version?: number; run_id?: string });
}

function nodeTransform(id: string): string {
  const inner = screen.getByTestId(`workflow-node-${id}`);
  const wrapper = inner.closest(".react-flow__node");
  if (!(wrapper instanceof HTMLElement)) throw new Error(`no React Flow node for ${id}`);
  if (!wrapper.style.transform) throw new Error(`${id} has no transform (${wrapper.getAttribute("style")})`);
  return wrapper.style.transform;
}

function parseTranslate(transform: string): { x: number; y: number } {
  const match = /translate\(([-\d.]+)px,\s*([-\d.]+)px\)/.exec(transform);
  if (!match) throw new Error(`unparsed transform ${transform}`);
  return { x: Number(match[1]), y: Number(match[2]) };
}

const originalBox = {
  offsetWidth: Object.getOwnPropertyDescriptor(HTMLElement.prototype, "offsetWidth"),
  offsetHeight: Object.getOwnPropertyDescriptor(HTMLElement.prototype, "offsetHeight"),
};
type MeasurableSvg = { getBBox?: () => DOMRect };
const svgProto = SVGElement.prototype as unknown as MeasurableSvg;
const originalGetBBox = svgProto.getBBox;

beforeAll(() => {
  // React Flow draws an edge only after the node has a measured box and handle bounds.
  // jsdom reports 0×0 and never fires ResizeObserver, so the test supplies both.
  class RO {
    constructor(private readonly callback: ResizeObserverCallback) {}
    observe(target: Element) {
      // Fire after the current effect flush. A sync callback runs before React Flow
      // stores the pane, and updateNodeInternals then no-ops for the rest of the mount.
      queueMicrotask(() => {
        const width = target instanceof HTMLElement ? target.offsetWidth : 0;
        const height = target instanceof HTMLElement ? target.offsetHeight : 0;
        const contentRect = {
          x: 0, y: 0, top: 0, left: 0, right: width, bottom: height, width, height,
          toJSON() { return {}; },
        };
        this.callback([{ target, contentRect } as ResizeObserverEntry], this as unknown as ResizeObserver);
      });
    }
    unobserve() {}
    disconnect() {}
  }
  vi.stubGlobal("ResizeObserver", RO);
  vi.stubGlobal("DOMMatrixReadOnly", class { m22 = 1; });
  // jsdom has no SVG text metrics. Edge labels still render; this only stops the measure effect from throwing.
  svgProto.getBBox = () => ({ x: 0, y: 0, width: 48, height: 12 }) as DOMRect;
  for (const prop of ["offsetWidth", "offsetHeight"] as const) {
    Object.defineProperty(HTMLElement.prototype, prop, {
      configurable: true,
      get() {
        if (this.classList?.contains("react-flow__node")) return prop === "offsetWidth" ? 200 : 64;
        if (this.classList?.contains("react-flow__handle")) return 8;
        return 0;
      },
    });
  }
});

afterAll(() => {
  for (const prop of ["offsetWidth", "offsetHeight"] as const) {
    const descriptor = originalBox[prop];
    if (descriptor) Object.defineProperty(HTMLElement.prototype, prop, descriptor);
  }
  if (originalGetBBox) svgProto.getBBox = originalGetBBox;
  else delete svgProto.getBBox;
});

beforeEach(() => {
  spec = publishedSpec;
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

async function renderDetail() {
  render(<WorkflowDetailPage />);
  await screen.findByTestId("workflow-graph");
}

describe("Workflows list acceptance", () => {
  it("lists each workflow on its own row and marks an unpublished workflow Draft only", async () => {
    render(<WorkflowsPage />);
    const table = await screen.findByRole("table", { name: "Workflows" });
    const published = await screen.findByTestId("workflow-row-wf-1");
    const draft = screen.getByTestId("workflow-row-wf-2");

    expect(within(published).getByRole("link", { name: "Triage" })).toHaveAttribute("href", "/workflows/wf-1");
    expect(within(published).getByRole("link", { name: "Open Triage" })).toHaveAttribute("href", "/workflows/wf-1");
    expect(published).toHaveTextContent("v2");
    expect(published).not.toHaveTextContent("Draft only");

    expect(within(draft).getByRole("link", { name: "Draft" })).toHaveAttribute("href", "/workflows/wf-2");
    expect(within(draft).getByRole("link", { name: "Open Draft" })).toHaveAttribute("href", "/workflows/wf-2");
    expect(draft).toHaveTextContent("Draft only");
    expect(draft).not.toHaveTextContent("vnull");
    expect(within(table).getAllByRole("row")).toHaveLength(3);
  });
});

describe("Workflow graph acceptance", () => {
  it("draws agent, tool, branch and approval nodes with labelled case and default edges, stably", async () => {
    const ids = ["classify", "lookup", "route", "approve", "summarize"];
    const { unmount } = render(<WorkflowDetailPage />);
    await screen.findByTestId("workflow-graph");

    expect(screen.getByTestId("workflow-graph")).toHaveAttribute("role", "img");
    expect(screen.getByTestId("workflow-node-classify")).toHaveAttribute("data-step-type", "agent");
    expect(screen.getByTestId("workflow-node-lookup")).toHaveAttribute("data-step-type", "tool");
    expect(screen.getByTestId("workflow-node-route")).toHaveAttribute("data-step-type", "branch");
    expect(screen.getByTestId("workflow-node-approve")).toHaveAttribute("data-step-type", "approval");
    expect(screen.getByTestId("workflow-node-summarize")).toHaveAttribute("data-step-type", "agent");
    for (const id of ids) {
      expect(screen.getByTestId(`workflow-node-${id}`)).toHaveAttribute("data-state", "definition");
    }

    const route = screen.getByTestId("workflow-step-route");
    expect(route).toHaveTextContent("1: input.urgent eq true");
    expect(route).toHaveTextContent("default");
    expect(route).toHaveTextContent("→ approve (1: input.urgent eq true), → summarize (default)");
    expect(screen.getByTestId("workflow-step-classify")).toHaveTextContent("→ lookup");
    expect(screen.getByTestId("workflow-step-lookup")).toHaveTextContent("→ route");
    expect(screen.getByTestId("workflow-step-approve")).toHaveTextContent("→ summarize");

    const graph = buildWorkflowGraph(spec);
    for (const edge of graph.edges) {
      expect(screen.getByTestId(`rf__edge-${edge.id}`)).toHaveAttribute("aria-label", `Edge from ${edge.source} to ${edge.target}`);
    }
    const edgeLabels = [...document.querySelectorAll(".react-flow__edge-text")].map((node) => node.textContent);
    expect(edgeLabels).toEqual(expect.arrayContaining(["1: input.urgent eq true", "default"]));

    const first = Object.fromEntries(ids.map((id) => [id, nodeTransform(id)]));
    const classify = parseTranslate(first.classify);
    const lookup = parseTranslate(first.lookup);
    const branch = parseTranslate(first.route);
    const approval = parseTranslate(first.approve);
    const summarize = parseTranslate(first.summarize);
    expect(classify.y).toBeLessThan(lookup.y);
    expect(lookup.y).toBeLessThan(branch.y);
    expect(branch.y).toBeLessThan(approval.y);
    expect(approval.y).toBeLessThan(summarize.y);
    expect(classify.x).toBe(summarize.x);

    unmount();
    render(<WorkflowDetailPage />);
    await screen.findByTestId("workflow-graph");
    const second = Object.fromEntries(ids.map((id) => [id, nodeTransform(id)]));
    expect(second).toEqual(first);
  });

  it("draws parallel fork and join edges and keeps those positions on a second render", async () => {
    spec = parallelSpec;
    const ids = ["fork", "a", "b", "a2", "merge"];
    const { unmount } = render(<WorkflowDetailPage />);
    await screen.findByTestId("workflow-graph");

    expect(screen.getByTestId("workflow-node-fork")).toHaveAttribute("data-step-type", "parallel");
    expect(screen.getByTestId("workflow-node-a")).toHaveAttribute("data-step-type", "tool");
    expect(screen.getByTestId("workflow-node-b")).toHaveAttribute("data-step-type", "tool");
    expect(screen.getByTestId("workflow-node-merge")).toHaveAttribute("data-step-type", "agent");
    expect(screen.getByTestId("workflow-step-fork")).toHaveTextContent("→ a, → b, → merge (join)");

    const graph = buildWorkflowGraph(parallelSpec);
    expect(graph.edges.filter((edge) => edge.kind === "fork").map((edge) => edge.target)).toEqual(["a", "b"]);
    expect(graph.edges.find((edge) => edge.kind === "join")).toMatchObject({ source: "fork", target: "merge", label: "join" });
    for (const edge of graph.edges) {
      expect(screen.getByTestId(`rf__edge-${edge.id}`)).toHaveAttribute("aria-label", `Edge from ${edge.source} to ${edge.target}`);
    }
    const edgeLabels = [...document.querySelectorAll(".react-flow__edge-text")].map((node) => node.textContent);
    expect(edgeLabels).toContain("join");

    const first = Object.fromEntries(ids.map((id) => [id, nodeTransform(id)]));
    const fork = parseTranslate(first.fork);
    const left = parseTranslate(first.a);
    const right = parseTranslate(first.b);
    const tail = parseTranslate(first.a2);
    const join = parseTranslate(first.merge);
    expect(fork.y).toBeLessThan(left.y);
    expect(left.y).toBe(right.y);
    expect(left.x).not.toBe(right.x);
    expect(tail.y).toBeGreaterThan(left.y);
    expect(join.y).toBeGreaterThan(tail.y);
    expect(join.y).toBeGreaterThan(right.y);

    unmount();
    render(<WorkflowDetailPage />);
    await screen.findByTestId("workflow-graph");
    expect(Object.fromEntries(ids.map((id) => [id, nodeTransform(id)]))).toEqual(first);
  });
});

describe("Workflow run panel acceptance", () => {
  it("switches between Form and JSON and posts the JSON object", async () => {
    await renderDetail();
    const form = screen.getByRole("button", { name: "Form" });
    const jsonMode = screen.getByRole("button", { name: "JSON" });
    expect(form).toHaveAttribute("aria-pressed", "true");
    expect(jsonMode).toHaveAttribute("aria-pressed", "false");
    expect(screen.getByLabelText("Text *")).toBeInTheDocument();
    expect(screen.getByLabelText("Urgent")).toBeInTheDocument();

    fireEvent.click(jsonMode);
    expect(jsonMode).toHaveAttribute("aria-pressed", "true");
    expect(form).toHaveAttribute("aria-pressed", "false");
    const raw = screen.getByTestId("workflow-input-json");
    fireEvent.change(raw, { target: { value: "[1]" } });
    expect(screen.getByRole("alert")).toHaveTextContent("Enter a valid JSON object");
    expect(screen.getByRole("button", { name: "Run workflow" })).toBeDisabled();

    fireEvent.change(raw, { target: { value: '{"text":"from-json","urgent":true}' } });
    expect(screen.queryByText("Enter a valid JSON object")).not.toBeInTheDocument();
    fireEvent.click(form);
    expect(screen.getByLabelText("Text *")).toHaveValue("from-json");
    expect(screen.getByLabelText("Urgent")).toBeChecked();

    fireEvent.click(jsonMode);
    runResponses.push((init) => {
      const sent = JSON.parse(String(init?.body)) as { run_id: string; version: number };
      return json(202, accepted(sent.run_id, sent.version));
    });
    fireEvent.click(screen.getByRole("button", { name: "Run workflow" }));
    await waitFor(() => expect(postBodies()).toHaveLength(1));
    const sent = postBodies()[0];
    expect(sent.input).toEqual({ text: "from-json", urgent: true });
    expect(sent.version).toBe(2);
    expect(sent.run_id).toEqual(expect.any(String));
    await waitFor(() => expect(push).toHaveBeenCalledWith(`/workflows/wf-1/runs/${sent.run_id}`));
  });

  it("maps a 422 list onto fields and leaves string request errors in the banner", async () => {
    await renderDetail();
    fireEvent.click(screen.getByRole("button", { name: "JSON" }));
    fireEvent.change(screen.getByTestId("workflow-input-json"), { target: { value: '{"text":"hi"}' } });
    runResponses.push(() => json(422, {
      detail: [
        { loc: ["body", "input", "text"], msg: "does not satisfy the input schema's 'required' keyword", type: "json_schema.required" },
        { loc: ["body", "input", "options", "retries"], msg: "Too small", type: "greater_than" },
        { loc: ["body", "input", "ghost"], msg: "Extra field", type: "extra" },
        { loc: ["body", "run_id"], msg: "bad id", type: "value_error" },
      ],
    }));
    fireEvent.click(screen.getByRole("button", { name: "Run workflow" }));

    expect(await screen.findByTestId("field-error-text")).toHaveTextContent("required");
    expect(screen.getByTestId("field-error-options.retries")).toHaveTextContent("Too small");
    expect(screen.getByTestId("field-errors-unmatched")).toHaveTextContent("ghost: Extra field");
    expect(screen.getByLabelText("Text *")).toHaveAttribute("aria-invalid", "true");
    expect(screen.getByLabelText("Retries")).toHaveAttribute("aria-invalid", "true");
    expect(screen.getByRole("button", { name: "Form" })).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByTestId("workflow-run-banner")).toHaveTextContent("body.run_id: bad id");
    expect(screen.queryByTestId("workflow-run-reuse-hint")).not.toBeInTheDocument();
    expect(push).not.toHaveBeenCalled();
  });

  it("shows string 422, 404 and 409 details in the banner and does not reuse the run id", async () => {
    await renderDetail();
    const refusals = [
      [422, "input does not satisfy the published schema"],
      [404, "Version not found"],
      [409, "Workflow has no published version"],
    ] as const;
    for (const [status, detail] of refusals) {
      runResponses.push(() => json(status, { detail }));
      fireEvent.click(screen.getByRole("button", { name: "Run workflow" }));
      expect(await screen.findByTestId("workflow-run-banner")).toHaveTextContent(detail);
      expect(screen.queryByTestId("workflow-run-reuse-hint")).not.toBeInTheDocument();
      expect(screen.getByRole("button", { name: "Run workflow" })).toBeEnabled();
    }
    const ids = postBodies().map((body) => body.run_id);
    expect(ids).toHaveLength(3);
    expect(new Set(ids).size).toBe(3);
    expect(push).not.toHaveBeenCalled();
  });

  it("reuses the run id after a 503 for the same version and input, and never retries by itself", async () => {
    await renderDetail();
    const refuse = () => json(503, { detail: "Run recorded but could not be started" });
    runResponses.push(refuse, refuse, refuse, refuse, refuse);

    fireEvent.change(screen.getByLabelText("Text *"), { target: { value: "hello" } });
    fireEvent.click(screen.getByRole("button", { name: "Run workflow" }));
    expect(await screen.findByTestId("workflow-run-banner")).toHaveTextContent("Run recorded but could not be started");
    expect(screen.getByTestId("workflow-run-reuse-hint")).toHaveAttribute("data-state", "not_started");
    expect(screen.getByTestId("workflow-run-reuse-hint")).toHaveTextContent("same run id");
    const idA = postBodies()[0].run_id;
    expect(workflowRunLedger.pending("wf-1", canonicalInputKey({ input: { text: "hello" }, version: 2 }))?.runId).toBe(idA);
    expect(workflowRunLedger.pending("wf-1", canonicalInputKey({ input: { text: "hello" }, version: 1 }))).toBeUndefined();

    await act(async () => { await new Promise((resolve) => setTimeout(resolve, 40)); });
    expect(postBodies()).toHaveLength(1);

    fireEvent.change(screen.getByLabelText("Text *"), { target: { value: "other" } });
    expect(screen.queryByTestId("workflow-run-reuse-hint")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Run workflow" }));
    await waitFor(() => expect(postBodies()).toHaveLength(2));
    const idB = postBodies()[1].run_id;
    expect(idB).not.toBe(idA);
    expect(postBodies()[1].input).toEqual({ text: "other" });
    expect(workflowRunLedger.pending("wf-1", canonicalInputKey({ input: { text: "other" }, version: 2 }))?.runId).toBe(idB);

    fireEvent.change(screen.getByLabelText("Text *"), { target: { value: "hello" } });
    expect(screen.getByTestId("workflow-run-reuse-hint")).toHaveAttribute("data-state", "not_started");
    fireEvent.click(screen.getByRole("button", { name: "Run workflow" }));
    await waitFor(() => expect(postBodies()).toHaveLength(3));
    expect(postBodies()[2].run_id).toBe(idA);
    expect(postBodies()[2]).toMatchObject({ input: { text: "hello" }, version: 2 });

    fireEvent.change(screen.getByLabelText("Version"), { target: { value: "1" } });
    await waitFor(() => expect(screen.getByLabelText("Text *")).toHaveValue(""));
    expect(screen.queryByTestId("workflow-run-reuse-hint")).not.toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Text *"), { target: { value: "hello" } });
    expect(screen.queryByTestId("workflow-run-reuse-hint")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Run workflow" }));
    await waitFor(() => expect(postBodies()).toHaveLength(4));
    const idC = postBodies()[3].run_id;
    expect(idC).not.toBe(idA);
    expect(postBodies()[3]).toMatchObject({ input: { text: "hello" }, version: 1 });

    fireEvent.change(screen.getByLabelText("Version"), { target: { value: "2" } });
    await waitFor(() => expect(screen.getByLabelText("Text *")).toHaveValue(""));
    fireEvent.change(screen.getByLabelText("Text *"), { target: { value: "hello" } });
    expect(screen.getByTestId("workflow-run-reuse-hint")).toHaveAttribute("data-state", "not_started");
    fireEvent.click(screen.getByRole("button", { name: "Run workflow" }));
    await waitFor(() => expect(postBodies()).toHaveLength(5));
    expect(postBodies()[4]).toMatchObject({ input: { text: "hello" }, version: 2, run_id: idA });
    expect(push).not.toHaveBeenCalled();
  });

  it("reuses the run id after a lost response and does not retry until the next click", async () => {
    await renderDetail();
    fireEvent.change(screen.getByLabelText("Text *"), { target: { value: "hello" } });
    runResponses.push(() => Promise.reject(new TypeError("Failed to fetch")));
    fireEvent.click(screen.getByRole("button", { name: "Run workflow" }));
    expect(await screen.findByTestId("workflow-run-banner")).toHaveTextContent("reuse the same run id");
    expect(screen.getByTestId("workflow-run-reuse-hint")).toHaveAttribute("data-state", "uncertain");
    const first = postBodies()[0];
    expect(first).toMatchObject({ input: { text: "hello" }, version: 2 });

    await act(async () => { await new Promise((resolve) => setTimeout(resolve, 40)); });
    expect(postBodies()).toHaveLength(1);

    runResponses.push((init) => {
      const sent = JSON.parse(String(init?.body)) as { run_id: string; version: number };
      return json(202, accepted(sent.run_id, sent.version));
    });
    fireEvent.click(screen.getByRole("button", { name: "Run workflow" }));
    await waitFor(() => expect(postBodies()).toHaveLength(2));
    expect(postBodies()[1]).toEqual({ input: { text: "hello" }, version: 2, run_id: first.run_id });
    await waitFor(() => expect(push).toHaveBeenCalledWith(`/workflows/wf-1/runs/${first.run_id}`));
  });
});
