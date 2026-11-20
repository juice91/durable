// The Metrics Overview pieces (SL.7; the former components/dashboard tests).
import { describe, expect, it, vi } from "vitest";
import React from "react";
import { fireEvent, render, screen, within } from "@testing-library/react";
import { MetricsOverview, RecentFailures, runLabel, type MetricsOverviewProps } from "./index";
import { ALL_KPIS, kpiCards } from "@/components/metrics";
import { RANGE, breakdownFixture, summaryFixture } from "@/components/metrics/metricsFixtures";
import { parsePageState } from "@/lib/metrics";
import type { TraceRun } from "@/lib/traces";
import { routes } from "@/lib/routes";

const filters = { ...RANGE };
const state = parsePageState(new URLSearchParams("range=30d"));

function run(overrides: Partial<TraceRun> = {}): TraceRun {
  return {
    run_id: "r-1", kind: "agent", agent_id: "a-1", version: 1, status: "failed", display_status: "failed",
    input: "hi", output: null, error: "boom", heartbeat_at: null, created_at: "2026-11-12T10:00:00Z",
    updated_at: "2026-11-12T10:00:01Z", trace_id: null, conversation_id: null, turn: null,
    ...overrides,
  };
}

const SUMMARY = summaryFixture(
  { runs: 10, completed: 6, failed: 3, needs_attention: 1, error_rate: 0.3, latency_p95_ms: 2500, input_tokens: 1200, output_tokens: 300, requests: 8, cost_usd: "0.0420000000" },
  { runs: 5, completed: 5, error_rate: 0, latency_p95_ms: 2000, input_tokens: 100, output_tokens: 50, requests: 4, cost_usd: "0.0100000000" },
);
const idle = { data: null, loading: false, error: null };

function overview(props: Partial<MetricsOverviewProps> = {}) {
  return (
    <MetricsOverview
      state={state}
      filters={filters}
      summary={{ ...idle, data: SUMMARY }}
      failures={{ ...idle, data: [] }}
      topAgents={{ ...idle, data: breakdownFixture("agent", [{ key: "a-1", label: "Support bot", runs: 4, failed: 1 }]).items }}
      agentSort="tokens"
      onAgentSortChange={() => undefined}
      {...props}
    />
  );
}

describe("MetricsOverview: recovered runs tile", () => {
  it("shows the summary's recovered_runs and links to Traces with the page's range", () => {
    const summary = summaryFixture({ runs: 10, recovered_runs: 3 }, { runs: 5, recovered_runs: 1 });
    render(overview({ summary: { ...idle, data: summary } }));
    const tile = screen.getByTestId("kpi-recovered");
    expect(screen.getByTestId("kpi-recovered-value")).toHaveTextContent("3");
    expect(tile).toHaveTextContent("Recovered runs");
    expect(tile.getAttribute("href")).toMatch(/^\/traces\?/);
    expect(tile.getAttribute("href")).toContain("from=");
  });

  it("reads 0 from an API that does not report it", () => {
    render(overview());
    expect(screen.getByTestId("kpi-recovered-value")).toHaveTextContent("0");
  });
});

describe("MetricsOverview: the KPI row", () => {
  it("shows every Metrics KPI with the Metrics page's values and deltas", () => {
    render(overview());
    const metrics = kpiCards(SUMMARY, filters, ALL_KPIS);
    for (const card of metrics) {
      expect(screen.getByTestId(`kpi-${card.id}-value`)).toHaveTextContent(card.value);
      if (card.delta) expect(screen.getByTestId(`kpi-${card.id}-delta`)).toHaveTextContent(card.delta.text);
    }
    expect(screen.getByTestId("kpi-runs-value")).toHaveTextContent("10");
  });

  it("runs and errors link to Traces with the page's range; the other cards are plain", () => {
    render(overview());
    expect(screen.getByTestId("kpi-runs").getAttribute("href")).toMatch(/^\/traces\?/);
    expect(screen.getByTestId("kpi-error_rate").getAttribute("href")).toMatch(/^\/traces\?status=failed&/);
    for (const id of ["latency_p50", "latency_p95", "tokens", "cost"]) expect(screen.getByTestId(`kpi-${id}`).tagName).toBe("DIV");
  });

  it("renders loading placeholders without a summary, and the summary error instead of the cards", () => {
    const { rerender } = render(overview({ summary: { data: null, loading: true, error: null } }));
    expect(screen.getByLabelText("Loading Runs")).toBeInTheDocument();
    expect(screen.queryByTestId("kpi-runs-value")).toBeNull();
    rerender(overview({ summary: { data: null, loading: false, error: "HTTP 500" } }));
    expect(within(screen.getByTestId("metrics-overview")).getAllByRole("alert")[0]).toHaveTextContent("HTTP 500");
    expect(screen.queryByTestId("kpi-runs")).toBeNull();
  });

  it("the KPI grid auto-fits (layout rule 3), no fixed column count", () => {
    render(overview());
    const grid = screen.getByTestId("kpi-runs").parentElement!;
    expect(grid.className).toContain("grid-cols-[repeat(auto-fit,minmax(min(100%,11rem),1fr))]");
    expect(grid.className).not.toMatch(/(^|\s)(sm:|md:|lg:|xl:)?grid-cols-\d/);
  });
});

describe("MetricsOverview: Needs attention and Top agents", () => {
  it("counts failed plus needs-attention runs of the summary in 'View all'", () => {
    render(overview());
    expect(screen.getByTestId("recent-failures-all")).toHaveTextContent("View all 4 in Traces");
    const all = new URL(screen.getByTestId("recent-failures-all").getAttribute("href")!, "http://x");
    expect(all.pathname).toBe(routes.traces());
    expect(all.searchParams.get("status")).toBe("failed,needs_attention");
  });

  it("a Top agents row narrows Metrics to the agent; its failed count opens Traces; the sort is a control", () => {
    const onAgentSortChange = vi.fn();
    const onNavigate = vi.fn();
    render(overview({ onAgentSortChange, onNavigate }));
    const region = screen.getByRole("region", { name: "Top agents" });
    const link = within(region).getByRole("link", { name: "Support bot: show this agent's metrics" });
    expect(link).toHaveAttribute("href", routes.metrics({ range: "30d", agent_id: "a-1" }));
    const failed = new URL(within(region).getByRole("link", { name: "Support bot: open 1 failed runs" }).getAttribute("href")!, "http://x");
    expect(failed.pathname).toBe(routes.traces());
    fireEvent.click(within(screen.getByTestId("breakdown-row-agent-a-1")).getByText("4"));
    expect(onNavigate).toHaveBeenCalledWith(routes.metrics({ range: "30d", agent_id: "a-1" }));
    fireEvent.change(within(region).getByLabelText("Top by"), { target: { value: "runs" } });
    expect(onAgentSortChange).toHaveBeenCalledWith("runs");
  });

  it("a row for the agent the page is already narrowed to opens its runs in Traces, not the current URL", () => {
    const narrowed = parsePageState(new URLSearchParams("range=30d&agent_id=a-1"));
    render(overview({ state: narrowed, filters: { ...filters, agent_id: "a-1" } }));
    const link = screen.getByRole("link", { name: "Support bot: open matching runs" });
    const url = new URL(link.getAttribute("href")!, "http://x");
    expect(url.pathname).toBe(routes.traces());
    expect(url.searchParams.get("agent_id")).toBe("a-1");
    expect(screen.queryByRole("link", { name: /show this agent's metrics/ })).toBeNull();
  });

  it("panel titles are h3 under the Overview's h2", () => {
    render(overview());
    expect(screen.getByRole("heading", { level: 2, name: "Overview" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { level: 3, name: "Needs attention" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { level: 3, name: "Top agents" })).toBeInTheDocument();
  });

  it("'Show 10' appears when 5 agents came back and toggles the limit; never with fewer than 5", () => {
    const five = breakdownFixture("agent", [1, 2, 3, 4, 5].map((n) => ({ key: `a-${n}`, label: `Agent ${n}`, runs: n }))).items;
    const onAgentLimitChange = vi.fn();
    const { rerender } = render(overview({ topAgents: { ...idle, data: five }, onAgentLimitChange }));
    const toggle = screen.getByTestId("top-agents-limit");
    expect(toggle).toHaveTextContent("Show 10");
    expect(toggle).toHaveAttribute("aria-pressed", "false");
    fireEvent.click(toggle);
    expect(onAgentLimitChange).toHaveBeenCalledWith(10);
    rerender(overview({ topAgents: { ...idle, data: five }, onAgentLimitChange, agentLimit: 10 }));
    expect(screen.getByTestId("top-agents-limit")).toHaveTextContent("Show 5");
    expect(screen.getByTestId("top-agents-limit")).toHaveAttribute("aria-pressed", "true");
    fireEvent.click(screen.getByTestId("top-agents-limit"));
    expect(onAgentLimitChange).toHaveBeenLastCalledWith(5);
    rerender(overview({ onAgentLimitChange }));
    expect(screen.queryByTestId("top-agents-limit")).toBeNull();
  });

  it("explains agentless kinds when the kind filter is not agent", () => {
    render(overview({ state: parsePageState(new URLSearchParams("kind=tool")) }));
    expect(screen.getByText(/Tool and workflow runs have no agent/)).toBeInTheDocument();
  });

  it("shows the empty and error states of both panels", () => {
    const { rerender } = render(overview({ topAgents: { ...idle, data: [] } }));
    expect(screen.getByText("No agent runs in this period")).toBeInTheDocument();
    expect(screen.getByTestId("recent-failures-empty")).toBeInTheDocument();
    rerender(overview({ topAgents: { ...idle, error: "agents down" }, failures: { ...idle, error: "runs down" } }));
    expect(within(screen.getByRole("region", { name: "Top agents" })).getByRole("alert")).toHaveTextContent("agents down");
    expect(within(screen.getByTestId("recent-failures")).getByRole("alert")).toHaveTextContent("runs down");
  });
});

describe("runLabel", () => {
  it("names agents, workflows and tools without links", () => {
    expect(runLabel(run(), { "a-1": "Support bot" })).toBe("Support bot");
    expect(runLabel(run({ agent_id: "1234567890ab" }))).toBe("Agent 12345678...");
    expect(runLabel(run({ kind: "workflow", agent_id: null, workflow_id: "w-1" }), {}, { "w-1": "Intake" })).toBe("Workflow: Intake");
    expect(runLabel(run({ kind: "tool", agent_id: null, tool_id: "http.get" }))).toBe("Tool: http.get");
  });
  it("labels eval runs with the target and suite name when available", () => {
    expect(
      runLabel(
        run({ kind: "eval", agent_id: null, workflow_id: "wf-1" }),
        {},
        { "wf-1": "Triage" },
      ),
    ).toBe("Test run: Triage");
    expect(runLabel(run({ kind: "eval", agent_id: null, workflow_id: "1234567890abcdef" }))).toBe("Test run: Workflow 12345678...");
    // Eval run with agent target
    expect(
      runLabel(
        run({ kind: "eval", agent_id: "a-1" }),
        { "a-1": "Support bot" },
      ),
    ).toBe("Test run: Support bot");
    expect(runLabel(run({ kind: "eval", agent_id: "a-1" }))).toBe("Test run: Agent a-1");
    // Eval run with suite name
    expect(
      runLabel(
        run({ kind: "eval", agent_id: null, workflow_id: "wf-1", suite_name: "Q4 suite" }),
        {},
        { "wf-1": "Triage" },
      ),
    ).toBe("Test run: Triage · Q4 suite");
    // Eval run without target (shouldn't happen in practice, but handle it)
    expect(runLabel(run({ kind: "eval", agent_id: null, workflow_id: null }))).toBe("Test run");
  });
  it("labels kb_source_sync runs with source_name when available", () => {
    // kb_source_sync with source_name: shows "KB source sync (<source_name>)"
    const syncRunWithSource = run({ kind: "kb_source_sync", agent_id: null, run_id: "fe7b6ff3", source_name: "ti-catalogue" });
    expect(runLabel(syncRunWithSource)).toBe("KB source sync (ti-catalogue)");
    // kb_source_sync without source_name: shows short run id
    const syncRunWithoutSource = run({ kind: "kb_source_sync", agent_id: null, knowledge_base_id: "kb-1", run_id: "srcsync-12345678" });
    expect(runLabel(syncRunWithoutSource)).toBe("KB source sync srcsync-...");
  });
});

describe("RecentFailures", () => {
  it("is titled Needs attention and lists runs newest first, each a single link to its trace", () => {
    render(
      <RecentFailures
        runs={[run(), run({ run_id: "r-2", display_status: "needs_attention", status: "needs_attention", error: null })]}
        total={7}
        viewAllHref={routes.traces({ status: "failed,needs_attention" })}
        agentNames={{ "a-1": "Support bot" }}
      />,
    );
    expect(screen.getByRole("region", { name: "Needs attention" })).toBe(screen.getByTestId("recent-failures"));
    const rows = screen.getAllByTestId("recent-failure");
    expect(rows.map((r) => r.getAttribute("href"))).toEqual([routes.trace("r-1"), routes.trace("r-2")]);
    expect(within(rows[0]).getByText("boom")).toBeInTheDocument();
    expect(within(rows[0]).queryAllByRole("link")).toHaveLength(0);
    expect(within(rows[1]).getByText("No error message")).toBeInTheDocument();
    expect(within(rows[1]).getByText("Needs Attention")).toBeInTheDocument();
    expect(screen.getByTestId("recent-failures-all")).toHaveTextContent("View all 7 in Traces");
    expect(screen.getByTestId("recent-failures-all")).toHaveAttribute("href", routes.traces({ status: "failed,needs_attention" }));
  });

  it("labels eval runs with the target and suite name when available", () => {
    const workflowEvalRun = run({
      kind: "eval",
      agent_id: null,
      workflow_id: "wf-1",
      suite_name: "Q4 suite",
    });
    const agentEvalRun = run({
      kind: "eval",
      agent_id: "a-1",
      workflow_id: null,
      suite_name: "Agent eval suite",
    });

    render(
      <RecentFailures
        runs={[workflowEvalRun, agentEvalRun]}
        total={2}
        viewAllHref={routes.traces()}
        agentNames={{ "a-1": "Support bot" }}
        workflowNames={{ "wf-1": "Triage" }}
      />,
    );
    const rows = screen.getAllByTestId("recent-failure");
    // First row: workflow eval run with suite name (target is just the name)
    expect(within(rows[0]).getByText("Test run: Triage · Q4 suite")).toBeInTheDocument();
    // Second row: agent eval run with suite name (target is just the name)
    expect(within(rows[1]).getByText("Test run: Support bot · Agent eval suite")).toBeInTheDocument();
  });

  it("shows empty, loading and error states", () => {
    const { rerender } = render(<RecentFailures runs={[]} total={0} viewAllHref={routes.traces()} />);
    expect(screen.getByTestId("recent-failures-empty")).toHaveTextContent("No failed or needs-attention runs in this period.");
    rerender(<RecentFailures runs={null} total={null} viewAllHref={routes.traces()} loading />);
    expect(screen.getByLabelText("Loading runs that need attention")).toBeInTheDocument();
    expect(screen.getByTestId("recent-failures-all")).toHaveTextContent("View all in Traces");
    rerender(<RecentFailures runs={null} total={null} viewAllHref={routes.traces()} error="HTTP 500" />);
    expect(screen.getByRole("alert")).toHaveTextContent("HTTP 500");
  });
});
