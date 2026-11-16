import React from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import "@testing-library/jest-dom";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { TooltipProvider } from "@/components/ui/Tooltip";
import { DatasetDetail } from "./DatasetDetail";

const renderDetail = () => render(<TooltipProvider><DatasetDetail suiteId="s1" /></TooltipProvider>);

vi.mock("next/navigation", () => ({
  usePathname: () => "/datasets/s1",
  useParams: () => ({}),
  useRouter: () => ({ push: vi.fn(), replace: vi.fn(), back: vi.fn(), prefetch: vi.fn() }),
  useSearchParams: () => new URLSearchParams(),
}));

const T = "2026-11-16T10:00:00Z";
const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
const caseOf = (n: number) => ({ id: `c${n}`, suite_id: "s1", name: `Case ${n}`, input: `input ${n}`, expected_output: null, metadata: {}, evaluators: [], position: n, created_at: T, updated_at: T });
const suiteOf = (over: Record<string, unknown> = {}) => ({
  id: "s1", agent_id: "a1", workflow_id: null, target_kind: "agent", target_id: "a1", target_name: "Support bot", name: "Refunds", description: null,
  evaluators: [{ kind: "contains", value: "refund" }], case_count: 1, archived: false, archived_at: null, created_at: T, updated_at: T, cases: [caseOf(1)], ...over,
});
const workflowSuite = () =>
  suiteOf({ agent_id: null, workflow_id: "wf1", target_kind: "workflow", target_id: "wf1", target_name: "Triage", cases: [caseOf(1)] });

let handlers: Record<string, (req: { url: URL; init?: RequestInit }) => Response | Promise<Response>>;
let calls: Array<{ method: string; path: string; query: URLSearchParams; body: unknown; headers: Record<string, string> }>;

beforeEach(() => {
  calls = [];
  handlers = {
    "/eval-suites/s1": () => json(suiteOf()),
    "/agents/a1": () => json({ id: "a1", name: "Support bot", latest_version: 1, draft_spec: { name: "Support bot", instructions: "x", model: { provider: "test", name: "echo" } } }),
    "/agents/a1/versions": () => json([]),
    "/agents/a1/evals/runs": () => json({ items: [], next_cursor: null }),
    "/workflows/wf1": () => json({ id: "wf1", name: "Triage", draft_spec: null, latest_version: 2 }),
    "/workflows/wf1/versions": () => json([{ workflow_id: "wf1", version: 2, spec: {}, spec_hash: "h", created_at: T, created_by: "u" }]),
    "/workflows/wf1/versions/2": () => json({ workflow_id: "wf1", version: 2, spec_hash: "h", created_at: T, created_by: "u", spec: { name: "Triage", input_schema: { type: "object", required: ["q"], properties: { q: { type: "string" } } }, start: "a", steps: [] } }),
    "/eval-suites/s1/import": () => json({ created: 1, updated: 0, suite: suiteOf() }),
  };
  vi.stubEnv("NEXT_PUBLIC_EAP_DEV_TOKEN", "test-token");
  vi.stubGlobal("fetch", vi.fn(async (input: unknown, init?: RequestInit) => {
    const url = new URL(String(input), "http://localhost");
    const path = url.pathname.replace("/api/v1", "");
    calls.push({
      method: init?.method ?? "GET", path, query: url.searchParams, body: typeof init?.body === "string" ? init.body : null,
      headers: Object.fromEntries(Object.entries((init?.headers ?? {}) as Record<string, string>).map(([k, v]) => [k.toLowerCase(), v])),
    });
    const handler = handlers[path];
    return handler ? handler({ url, init }) : json({ detail: "missing" }, 404);
  }));
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.unstubAllEnvs();
});

describe("DatasetDetail", () => {
  it("shows the dataset with New item, Import CSV, Import JSON and Schema", async () => {
    renderDetail();
    expect(await screen.findByTestId("eval-suite-title")).toHaveTextContent("Refunds");
    expect(screen.getByTestId("dataset-target-line")).toHaveTextContent("agent · Support bot");
    for (const name of ["New item", "Import CSV", "Import JSON", "Schema"]) {
      expect(screen.getByRole("button", { name })).toBeInTheDocument();
    }
    expect(screen.getByRole("link", { name: /Suites/ })).toHaveAttribute("href", "/datasets");
    expect(await screen.findByText("Case 1")).toBeInTheDocument();
  });

  it("opens the Schema sheet with the evaluators of an agent dataset", async () => {
    renderDetail();
    await screen.findByText("Case 1");
    fireEvent.click(screen.getByRole("button", { name: "Schema" }));
    const sheet = await screen.findByTestId("eval-schema-sheet");
    expect(within(sheet).getByTestId("eval-schema-evaluators")).toHaveTextContent("Contains");
    expect(within(sheet).queryByTestId("eval-schema-input")).toBeNull();
  });

  it("shows the workflow's input schema in the Schema sheet of a workflow dataset", async () => {
    handlers["/eval-suites/s1"] = () => json(workflowSuite());
    renderDetail();
    await screen.findByText("Case 1");
    expect(screen.getByTestId("dataset-target-line")).toHaveTextContent("workflow · Triage");
    fireEvent.click(screen.getByRole("button", { name: "Schema" }));
    const schema = await screen.findByTestId("eval-schema-input");
    expect(JSON.parse(schema.textContent ?? "")).toMatchObject({ type: "object", required: ["q"] });
    expect(calls.some((c) => c.path === "/agents/a1")).toBe(false);
  });

  it("Import CSV posts the file with format=csv and refreshes", async () => {
    renderDetail();
    await screen.findByText("Case 1");
    const input = screen.getByTestId("eval-suite-import-file") as HTMLInputElement;
    const file = new File(["name,input\na,b\n"], "items.csv", { type: "text/csv" });
    Object.defineProperty(file, "text", { value: async () => "name,input\na,b\n" });
    fireEvent.change(input, { target: { files: [file] } });
    await waitFor(() => expect(calls.filter((c) => c.method === "POST" && c.path === "/eval-suites/s1/import")).toHaveLength(1));
    const post = calls.find((c) => c.method === "POST" && c.path === "/eval-suites/s1/import")!;
    expect(post.query.get("format")).toBe("csv");
    expect(post.headers["content-type"]).toBe("text/csv");
    expect(post.body).toBe("name,input\na,b\n");
  });

  it("shows a CSV problem as its row and message", async () => {
    handlers["/eval-suites/s1/import"] = () => json({ detail: { code: "csv_invalid", row: 3, message: "name is required" } }, 422);
    renderDetail();
    await screen.findByText("Case 1");
    const input = screen.getByTestId("eval-suite-import-file") as HTMLInputElement;
    const file = new File(["x"], "items.csv", { type: "text/csv" });
    Object.defineProperty(file, "text", { value: async () => "name,input\nok,hello\n,x\n" });
    fireEvent.change(input, { target: { files: [file] } });
    expect(await screen.findByTestId("eval-suite-import-error")).toHaveTextContent("Row 3: name is required");
  });

  it("says when the dataset does not exist", async () => {
    handlers["/eval-suites/s1"] = () => json({ detail: "Eval suite not found" }, 404);
    renderDetail();
    expect(await screen.findByText("Dataset not found.")).toBeInTheDocument();
  });
});
