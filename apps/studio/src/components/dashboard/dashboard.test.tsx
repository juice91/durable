import { describe, expect, it } from "vitest";
import React from "react";
import { render, screen, within } from "@testing-library/react";
import { DASHBOARD_KPIS, DashboardKpis, RecentFailures, dashboardKpiCards, runLabel } from "./index";
import { kpiCards } from "@/components/metrics";
import { RANGE, summaryFixture } from "@/components/metrics/metricsFixtures";
import { parseDashboardState } from "@/lib/dashboard";
import type { TraceRun } from "@/lib/traces";

const filters = { ...RANGE };
const state = parseDashboardState(new URLSearchParams("range=30d"));

function run(overrides: Partial<TraceRun> = {}): TraceRun {
  return {
    run_id: "r-1", kind: "agent", agent_id: "a-1", version: 1, status: "failed", display_status: "failed",
    input: "hi", output: null, error: "boom", heartbeat_at: null, created_at: "2026-11-12T10:00:00Z",
    updated_at: "2026-11-12T10:00:01Z", trace_id: null, conversation_id: null, turn: null,
    ...overrides,
  };
}

describe("dashboardKpiCards", () => {
  const summary = summaryFixture(
    { runs: 10, completed: 6, failed: 3, needs_attention: 1, error_rate: 0.3, latency_p95_ms: 2500, input_tokens: 1200, output_tokens: 300, requests: 8, cost_usd: "0.0420000000" },
    { runs: 5, completed: 5, error_rate: 0, latency_p95_ms: 2000, input_tokens: 100, output_tokens: 50, requests: 4, cost_usd: "0.0100000000" },
  );

  it("shows runs, error rate, p95, tokens and cost with the Metrics page's values and deltas", () => {
    const cards = dashboardKpiCards(summary, filters, state);
    expect(cards.map((c) => c.id)).toEqual(["runs", "error_rate", "latency_p95", "tokens", "cost"]);
    const metrics = kpiCards(summary, filters, DASHBOARD_KPIS);
    expect(cards.map(({ value, delta, label, hint }) => ({ value, delta, label, hint }))).toEqual(
      metrics.map(({ value, delta, label, hint }) => ({ value, delta, label, hint })),
    );
  });

  it("every card links: runs and errors to Traces, the rest to Metrics on the same range", () => {
    const hrefs = Object.fromEntries(dashboardKpiCards(summary, filters, state).map((c) => [c.id, c.href]));
    expect(hrefs.runs).toMatch(/^\/traces\?/);
    expect(hrefs.error_rate).toMatch(/^\/traces\?status=failed&/);
    expect(hrefs.latency_p95).toBe("/metrics?range=30d");
    expect(hrefs.tokens).toBe("/metrics?range=30d");
    expect(hrefs.cost).toBe("/metrics?range=30d");
  });

  it("renders five linked cards, or loading placeholders without a summary", () => {
    const { rerender } = render(<DashboardKpis summary={summary} filters={filters} state={state} />);
    for (const id of DASHBOARD_KPIS) expect(screen.getByTestId(`kpi-${id}`).tagName).toBe("A");
    expect(screen.getByTestId("kpi-runs-value")).toHaveTextContent("10");
    rerender(<DashboardKpis summary={null} filters={filters} state={state} loading />);
    expect(screen.getByLabelText("Loading Runs")).toBeInTheDocument();
    expect(screen.queryByRole("link")).toBeNull();
  });
});

describe("runLabel", () => {
  it("names agents, workflows and tools without links", () => {
    expect(runLabel(run(), { "a-1": "Support bot" })).toBe("Support bot");
    expect(runLabel(run({ agent_id: "1234567890ab" }))).toBe("Agent 12345678...");
    expect(runLabel(run({ kind: "workflow", agent_id: null, workflow_id: "w-1" }), {}, { "w-1": "Intake" })).toBe("Workflow: Intake");
    expect(runLabel(run({ kind: "tool", agent_id: null, tool_id: "http.get" }))).toBe("Tool: http.get");
  });
});

describe("RecentFailures", () => {
  it("lists runs newest first, each a single link to its trace", () => {
    render(
      <RecentFailures
        runs={[run(), run({ run_id: "r-2", display_status: "needs_attention", status: "needs_attention", error: null })]}
        total={7}
        viewAllHref="/traces?status=failed%2Cneeds_attention"
        agentNames={{ "a-1": "Support bot" }}
      />,
    );
    const rows = screen.getAllByTestId("recent-failure");
    expect(rows.map((r) => r.getAttribute("href"))).toEqual(["/traces/r-1", "/traces/r-2"]);
    expect(within(rows[0]).getByText("boom")).toBeInTheDocument();
    expect(within(rows[0]).queryAllByRole("link")).toHaveLength(0);
    expect(within(rows[1]).getByText("No error message")).toBeInTheDocument();
    expect(within(rows[1]).getByText("Needs Attention")).toBeInTheDocument();
    expect(screen.getByTestId("recent-failures-all")).toHaveTextContent("View all 7 in Traces");
    expect(screen.getByTestId("recent-failures-all")).toHaveAttribute("href", "/traces?status=failed%2Cneeds_attention");
  });

  it("shows empty, loading and error states", () => {
    const { rerender } = render(<RecentFailures runs={[]} total={0} viewAllHref="/traces" />);
    expect(screen.getByTestId("recent-failures-empty")).toHaveTextContent("No failed or needs-attention runs in this period.");
    rerender(<RecentFailures runs={null} total={null} viewAllHref="/traces" loading />);
    expect(screen.getByLabelText("Loading recent failures")).toBeInTheDocument();
    expect(screen.getByTestId("recent-failures-all")).toHaveTextContent("View all in Traces");
    rerender(<RecentFailures runs={null} total={null} viewAllHref="/traces" error="HTTP 500" />);
    expect(screen.getByRole("alert")).toHaveTextContent("HTTP 500");
  });
});
