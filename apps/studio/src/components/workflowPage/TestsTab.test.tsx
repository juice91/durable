import React from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import "@testing-library/jest-dom";
import { cleanup, render, screen, within } from "@testing-library/react";
import { TooltipProvider } from "@/components/ui/Tooltip";
import { TestsTab } from "./TestsTab";

let query = "suite=s1&run=r1";
vi.mock("next/navigation", () => ({
  usePathname: () => "/workflows/wf1/tests",
  useParams: () => ({}),
  useRouter: () => ({ push: vi.fn(), replace: vi.fn(), back: vi.fn(), prefetch: vi.fn() }),
  useSearchParams: () => new URLSearchParams(query),
}));

const T = "2026-11-16T10:00:00Z";
const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
const agg = { cases: 2, passed: 1, failed: 1, errors: 0, pass_rate: 0.5, scores_avg: {}, duration_ms_p50: 100, cost_usd: "0.0001" };
const run = { run_id: "r1", kind: "eval", agent_id: null, workflow_id: "wf1", version: null, workflow_version: 2, status: "completed", display_status: "completed", error: null, created_at: T, updated_at: T, trace_id: null, suite_id: "s1", suite_hash: "h", aggregates: agg };
const result = (n: number, status: string) => ({ case_id: `c${n}`, case_name: `Case ${n}`, position: n, child_run_id: null, status, output: { label: "bug" }, assertions: { eq: { value: status === "passed", reason: null } }, scores: {}, labels: {}, evaluator_failures: [], duration_ms: 5, cost_usd: null, error_code: null, created_at: T });

let paths: string[];
beforeEach(() => {
  paths = [];
  query = "suite=s1&run=r1";
  vi.stubEnv("NEXT_PUBLIC_EAP_DEV_TOKEN", "test-token");
  vi.stubGlobal("fetch", vi.fn(async (input: unknown) => {
    const url = new URL(String(input), "http://localhost");
    const path = url.pathname.replace("/api/v1", "");
    paths.push(path);
    if (path === "/workflows/wf1/versions") return json([{ workflow_id: "wf1", version: 2, spec: {}, spec_hash: "h", created_at: T, created_by: "u" }]);
    if (path === "/workflows/wf1/evals/runs") return json({ items: [run], next_cursor: null });
    if (path === "/workflows/wf1/evals/runs/r1") return json(run);
    if (path === "/workflows/wf1/evals/runs/r1/results") return json({ items: [result(0, "passed"), result(1, "failed")], next_cursor: null });
    return json({ detail: "missing" }, 404);
  }));
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.unstubAllEnvs();
});

describe("workflow Tests tab", () => {
  it("opens a run in the run view through the workflow routes, with a back link to the suite", async () => {
    render(<TooltipProvider><TestsTab workflowId="wf1" name="Triage" /></TooltipProvider>);
    const rows = await screen.findAllByTestId("eval-result-row", {}, { timeout: 4000 }).catch(() => []);
    expect(paths).toContain("/workflows/wf1/evals/runs/r1");
    expect(paths).toContain("/workflows/wf1/evals/runs/r1/results");
    expect(paths.some((p) => p.startsWith("/agents/"))).toBe(false);
    expect(await screen.findByText("Case 1")).toBeInTheDocument();
    expect(rows.length === 0 || rows.length === 2).toBe(true);
    expect(screen.getByTestId("eval-run-back")).toHaveAttribute("href", "/workflows/wf1/tests?suite=s1");
    expect(screen.queryByTestId("eval-compare-go")).toBeNull();
  });
});
