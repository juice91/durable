import React from "react";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import type { WorkflowSpec } from "@/lib/workflows";

vi.mock("next/navigation", () => ({
  useParams: () => ({ id: "wf-1" }),
  useRouter: () => ({ push: vi.fn(), replace: vi.fn() }),
  useSearchParams: () => new URLSearchParams(),
  usePathname: () => "/workflows/wf-1",
}));
vi.mock("next/link", () => ({ default: ({ href, children, ...rest }: { href: string; children: React.ReactNode }) => <a href={href} {...rest}>{children}</a> }));

import WorkflowDetailPage from "./[id]/page";

const spec = {
  name: "Sync",
  input_schema: { type: "object" },
  steps: [
    { id: "fetch", type: "tool", tool_id: "http.request", input: { method: "GET", url: "https://example.com/news", extract_html: { mode: "links" } }, next: "write" },
    { id: "write", type: "agent", agent_id: "agent-1", version: 3, next: null },
  ],
} as unknown as WorkflowSpec;
const WF = { id: "wf-1", name: "Sync", draft_spec: spec, latest_version: 1 };
const V1 = { workflow_id: "wf-1", version: 1, spec, spec_hash: "h", created_at: "2026-11-13T09:00:00Z", created_by: "dev" };
const TOOL = { id: "http.request", title: "HTTP request", description: "Call an HTTPS endpoint.", side_effect: true, external_idempotent: false, input_schema: { type: "object" }, allowed: true };
const AGENT = { id: "agent-1", name: "Writer", draft_spec: {}, latest_version: 3, latest_spec_summary: { version: 3, model: { provider: "cloudflare", name: "deepseek" }, tool_count: 0, spec_hash: "x" } };

const json = (body: unknown) => Promise.resolve(new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } }));
function route(url: string): Promise<Response> {
  const p = new URL(url).pathname;
  if (p === "/api/v1/workflows/wf-1") return json(WF);
  if (p === "/api/v1/workflows/wf-1/versions") return json([V1]);
  if (p === "/api/v1/tools") return json([TOOL]);
  if (p === "/api/v1/agents") return json([AGENT]);
  if (p === "/api/v1/runs") return json({ items: [], next_before: null });
  return Promise.resolve(new Response(JSON.stringify({ detail: "not mocked" }), { status: 404 }));
}

beforeAll(() => {
  class RO { observe() {} unobserve() {} disconnect() {} }
  vi.stubGlobal("ResizeObserver", RO);
  vi.stubGlobal("DOMMatrixReadOnly", class { m22 = 1; });
});
beforeEach(() => {
  vi.stubEnv("NEXT_PUBLIC_EAP_DEV_TOKEN", "Bearer test");
  vi.stubGlobal("fetch", vi.fn(route));
});
afterEach(() => { cleanup(); vi.unstubAllEnvs(); });

describe("workflow Graph tab: step details", () => {
  it("selecting a step opens its definition card with the JSON and the tool declaration; Escape closes it", async () => {
    render(<WorkflowDetailPage />);
    await screen.findByTestId("workflow-graph");
    // The agent name arrives from the agents list.
    await waitFor(() => expect(within(screen.getByTestId("step-summary-write")).getByRole("link", { name: "Writer" })).toBeInTheDocument());
    expect(screen.queryByTestId("step-definition")).toBeNull();

    fireEvent.click(within(screen.getByTestId("workflow-step-fetch")).getByRole("button", { name: /Step fetch/ }));
    const card = await screen.findByTestId("step-definition");
    expect(JSON.parse(within(card).getByTestId("step-definition-json").textContent ?? "")).toMatchObject({ id: "fetch", tool_id: "http.request" });
    await waitFor(() => expect(within(card).getByTestId("step-definition-tool")).toHaveTextContent("HTTP request"));
    expect(card).toHaveTextContent("Call an HTTPS endpoint.");
    expect(within(card).getByTestId("step-definition-next")).toHaveTextContent("write");
    expect(within(card).getByTestId("step-definition-copy")).toBeInTheDocument();

    fireEvent.keyDown(document, { key: "Escape" });
    expect(screen.queryByTestId("step-definition")).toBeNull();
  });

  it("a canvas node click selects too; an agent step shows its name, version and model; Close clears", async () => {
    render(<WorkflowDetailPage />);
    await screen.findByTestId("workflow-graph");
    await waitFor(() => expect(screen.getByTestId("workflow-node-subtitle-write")).toHaveTextContent("Writer"));
    fireEvent.click(screen.getByTestId("workflow-node-write"));
    const card = await screen.findByTestId("step-definition");
    expect(within(card).getByTestId("step-definition-agent")).toHaveTextContent("Writer");
    expect(within(card).getByTestId("step-definition-agent")).toHaveTextContent("v3 · cloudflare/deepseek");
    fireEvent.click(within(card).getByTestId("step-definition-close"));
    expect(screen.queryByTestId("step-definition")).toBeNull();
  });
});
