import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import React from "react";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import DashboardPage from "./page";
import MetricsPage from "./metrics/page";
import { breakdownFixture, summaryFixture, timeseriesFixture } from "@/components/metrics/metricsFixtures";
import { DASHBOARD_KPIS } from "@/components/dashboard";
import { routes } from "@/lib/routes";

const push = vi.fn();
const replace = vi.fn();
let search = "";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push, replace }),
  useSearchParams: () => new URLSearchParams(search),
}));

vi.mock("@/lib/api", async (importOriginal) => {
  const original = await importOriginal<typeof import("@/lib/api")>();
  return { ...original, api: { ...original.api, agents: { ...original.api.agents, list: vi.fn().mockResolvedValue([{ id: "a-1", name: "Support bot" }]) } } };
});

type Responder = (url: URL) => { status: number; body: unknown };

const SUMMARY = summaryFixture(
  {
    runs: 12, completed: 7, failed: 3, needs_attention: 1, in_progress: 1, error_rate: 3 / 11,
    latency_p50_ms: 800, latency_p95_ms: 4200, requests: 9, input_tokens: 15000, output_tokens: 2500,
    cost_usd: "0.0420000000", unpriced_requests: 1,
  },
  { runs: 6, completed: 6, error_rate: 0, latency_p95_ms: 3000, requests: 4, input_tokens: 4000, output_tokens: 1000, cost_usd: "0.0100000000" },
);

const FAILED_RUN = {
  run_id: "run-failed-1", kind: "agent", agent_id: "a-1", version: 2, status: "failed", display_status: "failed",
  input: "hi", output: null, error: "model_error: upstream 500", heartbeat_at: null,
  created_at: "2026-11-12T09:00:00Z", updated_at: "2026-11-12T09:00:03Z", trace_id: null, conversation_id: null, turn: null,
};

const okResponder: Responder = (url) => {
  if (url.pathname === "/api/v1/models") return { status: 200, body: { items: [] } };
  if (url.pathname === "/api/v1/workflows") return { status: 200, body: [] };
  if (url.pathname === "/api/v1/runs") return { status: 200, body: { items: [FAILED_RUN], next_before: null } };
  if (url.pathname.endsWith("/summary")) return { status: 200, body: SUMMARY };
  if (url.pathname.endsWith("/timeseries")) {
    return { status: 200, body: timeseriesFixture([{ runs: 5, failed: 1, cost_usd: "0.02" }, { runs: 0 }, { runs: 7, failed: 2, cost_usd: "0.022" }]) };
  }
  if (url.searchParams.get("by") === "agent") {
    return { status: 200, body: breakdownFixture("agent", [{ key: "a-1", label: "Support bot", runs: 12, input_tokens: 15000, output_tokens: 2500 }]) };
  }
  return { status: 200, body: breakdownFixture("model", []) };
};

const emptyResponder: Responder = (url) => {
  if (url.pathname === "/api/v1/runs") return { status: 200, body: { items: [], next_before: null } };
  if (url.pathname.endsWith("/summary")) return { status: 200, body: summaryFixture({}, {}) };
  if (url.pathname.endsWith("/timeseries")) return { status: 200, body: timeseriesFixture([]) };
  if (url.pathname.endsWith("/breakdown")) return { status: 200, body: breakdownFixture("agent", []) };
  return okResponder(url);
};

let calls: URL[] = [];

function mockFetch(responder: Responder) {
  calls = [];
  vi.stubGlobal("fetch", vi.fn(async (input: string) => {
    const url = new URL(input);
    calls.push(url);
    const { status, body } = responder(url);
    return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
  }));
}

const callsTo = (suffix: string) => calls.filter((u) => u.pathname.endsWith(suffix));

describe("DashboardPage", () => {
  beforeEach(() => {
    vi.stubEnv("NEXT_PUBLIC_EAP_DEV_TOKEN", "Bearer test");
    push.mockReset();
    replace.mockReset();
    search = "";
  });
  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
    vi.unstubAllEnvs();
  });

  it("shows KPI cards with deltas, trends, top agents and recent failures from the API", async () => {
    mockFetch(okResponder);
    render(<DashboardPage />);
    await waitFor(() => expect(screen.getByTestId("kpi-runs-value")).toHaveTextContent("12"));
    expect(screen.getByTestId("kpi-error_rate-value")).toHaveTextContent("27.3%");
    expect(screen.getByTestId("kpi-latency_p95-value")).toHaveTextContent("4.2 s");
    expect(screen.getByTestId("kpi-tokens-value")).toHaveTextContent("17,500");
    expect(screen.getByTestId("kpi-cost-value")).toHaveTextContent("$0.042");
    expect(screen.getByTestId("kpi-runs-delta")).toHaveTextContent("up 100% from 6");
    expect(screen.queryByTestId("kpi-latency_p50")).toBeNull();
    expect(screen.getByText("Runs and failures")).toBeInTheDocument();
    expect(screen.getAllByText("Cost (USD)").length).toBeGreaterThan(0);
    expect(screen.queryByText("Latency")).toBeNull();
    await waitFor(() => expect(screen.getByTestId("recent-failure")).toHaveAttribute("href", routes.trace("run-failed-1")));
    expect(screen.getByTestId("recent-failure")).toHaveTextContent("Support bot");
    expect(screen.getByTestId("recent-failure")).toHaveTextContent("model_error: upstream 500");
    expect(screen.getByTestId("recent-failures-all")).toHaveTextContent("View all 4 in Traces");
    await waitFor(() => expect(screen.getByRole("link", { name: "Support bot: open in Metrics" })).toBeInTheDocument());
  });

  it("queries the range only: no dimension filters, top 5 agents by tokens, failures of the range", async () => {
    vi.useFakeTimers({ toFake: ["Date"] });
    vi.setSystemTime(new Date("2026-11-13T12:00:30Z"));
    search = "range=24h&agent_id=a-9";
    mockFetch(okResponder);
    render(<DashboardPage />);
    await waitFor(() => expect(callsTo("/api/v1/runs")).toHaveLength(1));
    const summary = callsTo("/summary")[0];
    expect(summary.searchParams.get("from")).toBe("2026-11-12T12:01:00.000Z");
    expect(summary.searchParams.get("to")).toBe("2026-11-13T12:01:00.000Z");
    expect(summary.searchParams.get("agent_id")).toBeNull();
    expect(summary.searchParams.get("compare")).toBe("previous");
    await waitFor(() => expect(callsTo("/breakdown")).toHaveLength(1));
    const breakdown = callsTo("/breakdown")[0];
    expect([breakdown.searchParams.get("by"), breakdown.searchParams.get("sort"), breakdown.searchParams.get("limit")]).toEqual(["agent", "tokens", "5"]);
    const runs = callsTo("/api/v1/runs")[0];
    expect(runs.searchParams.get("status")).toBe("failed,needs_attention");
    expect(runs.searchParams.get("from")).toBe("2026-11-12T12:01:00.000Z");
    expect(runs.searchParams.get("to")).toBe("2026-11-13T12:01:00.000Z");
    expect(runs.searchParams.get("limit")).toBe("5");
  });

  it("every element links to Metrics or Traces with the dashboard's filters", async () => {
    vi.useFakeTimers({ toFake: ["Date"] });
    vi.setSystemTime(new Date("2026-11-13T12:00:30Z"));
    search = "range=30d";
    mockFetch(okResponder);
    render(<DashboardPage />);
    await waitFor(() => expect(screen.getByTestId("kpi-runs")).toHaveAttribute("href"));
    expect(screen.getByTestId("dashboard-open-metrics")).toHaveAttribute("href", routes.metrics({ range: "30d" }));
    expect(screen.getByTestId("dashboard-trends-metrics")).toHaveAttribute("href", routes.metrics({ range: "30d" }));
    const traces = new URL(screen.getByTestId("kpi-runs").getAttribute("href")!, "http://x");
    expect(traces.pathname).toBe(routes.traces());
    expect(traces.searchParams.get("from")).toBe("2026-10-14T12:01:00.000Z");
    expect(traces.searchParams.get("to")).toBe("2026-11-13T12:01:00.000Z");
    expect(screen.getByTestId("kpi-error_rate").getAttribute("href")).toContain("status=failed");
    for (const id of ["latency_p95", "tokens", "cost"]) {
      expect(screen.getByTestId(`kpi-${id}`)).toHaveAttribute("href", routes.metrics({ range: "30d" }));
    }
    await waitFor(() => expect(screen.getByRole("link", { name: "Support bot: open in Metrics" })).toHaveAttribute("href", routes.metrics({ range: "30d", agent_id: "a-1" })));
    fireEvent.click(within(screen.getByTestId("breakdown-row-agent-a-1")).getByText("12"));
    expect(push).toHaveBeenCalledWith(routes.metrics({ range: "30d", agent_id: "a-1" }));
    const all = new URL(screen.getByTestId("recent-failures-all").getAttribute("href")!, "http://x");
    expect(all.pathname).toBe(routes.traces());
    expect(all.searchParams.get("status")).toBe("failed,needs_attention");
    expect(all.searchParams.get("from")).toBe("2026-10-14T12:01:00.000Z");
  });

  it("changing the date range updates the dashboard URL", async () => {
    mockFetch(okResponder);
    render(<DashboardPage />);
    fireEvent.change(screen.getByLabelText("Date range"), { target: { value: "30d" } });
    expect(replace).toHaveBeenCalledWith(routes.dashboard({ range: "30d" }), { scroll: false });
    fireEvent.change(screen.getByLabelText("Date range"), { target: { value: "7d" } });
    expect(replace).toHaveBeenLastCalledWith(routes.dashboard(), { scroll: false });
    expect(screen.queryByLabelText("Agent")).toBeNull();
    expect(screen.queryByLabelText("Interval")).toBeNull();
  });

  it("an empty tenant gets zero cards, empty panels and links to start a run", async () => {
    mockFetch(emptyResponder);
    render(<DashboardPage />);
    await waitFor(() => expect(screen.getByTestId("dashboard-empty")).toBeInTheDocument());
    expect(screen.getByTestId("kpi-runs-value")).toHaveTextContent("0");
    expect(screen.getByTestId("kpi-error_rate-value")).toHaveTextContent("—");
    const empty = screen.getByTestId("dashboard-empty");
    expect(within(empty).getByRole("link", { name: "Agents" })).toHaveAttribute("href", routes.agents());
    expect(within(empty).getByRole("link", { name: "Playground" })).toHaveAttribute("href", routes.playground());
    await waitFor(() => expect(screen.getByTestId("recent-failures-empty")).toBeInTheDocument());
    await waitFor(() => expect(screen.getByText("No agent runs in this period")).toBeInTheDocument());
  });

  it("shows the API error for the summary and for each panel", async () => {
    mockFetch((url) =>
      url.pathname.startsWith("/api/v1/metrics/") || url.pathname === "/api/v1/runs"
        ? { status: 500, body: { detail: "database unavailable" } }
        : okResponder(url),
    );
    render(<DashboardPage />);
    await waitFor(() => expect(screen.getAllByRole("alert").length).toBeGreaterThanOrEqual(3));
    for (const alert of screen.getAllByRole("alert")) expect(alert).toHaveTextContent("database unavailable");
    expect(within(screen.getByTestId("recent-failures")).getByRole("alert")).toBeInTheDocument();
  });

  it("an invalid custom range is refused without calling the API", async () => {
    search = "range=custom&from=2026-01-01T00:00:00Z&to=2026-11-01T00:00:00Z";
    mockFetch(okResponder);
    render(<DashboardPage />);
    expect(screen.getByRole("alert")).toHaveTextContent("the range must be at most 90 days");
    await Promise.resolve();
    expect(calls.filter((u) => u.pathname.startsWith("/api/v1/metrics/") || u.pathname === "/api/v1/runs")).toEqual([]);
  });
});

describe("Dashboard and Metrics page parity (P0.18 acceptance)", () => {
  beforeEach(() => {
    vi.stubEnv("NEXT_PUBLIC_EAP_DEV_TOKEN", "Bearer test");
    vi.useFakeTimers({ toFake: ["Date"] });
    vi.setSystemTime(new Date("2026-11-13T12:00:30Z"));
  });
  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
    vi.unstubAllEnvs();
  });

  async function renderAndRead(page: React.ReactElement) {
    mockFetch(okResponder);
    render(page);
    await waitFor(() => expect(screen.getByTestId("kpi-runs-value")).toHaveTextContent("12"));
    const values = Object.fromEntries(
      DASHBOARD_KPIS.map((id) => [id, {
        value: screen.getByTestId(`kpi-${id}-value`).textContent,
        delta: screen.queryByTestId(`kpi-${id}-delta`)?.textContent ?? null,
      }]),
    );
    const metricQuery = (suffix: string) => callsTo(suffix).map((u) => u.searchParams.toString());
    const queries = { summary: metricQuery("/summary"), timeseries: metricQuery("/timeseries") };
    cleanup();
    return { values, queries };
  }

  for (const qs of ["", "range=24h", "range=90d", "range=custom&from=2026-11-01T00:00:00Z&to=2026-11-05T00:00:00Z"]) {
    it(`same range (${qs || "default"}): same metrics queries and the same KPI numbers`, async () => {
      search = qs;
      const dashboard = await renderAndRead(<DashboardPage />);
      // The dashboard's own "Open in Metrics" link is the Metrics URL for this range.
      search = qs;
      const metrics = await renderAndRead(<MetricsPage />);
      expect(dashboard.queries.summary).toEqual(metrics.queries.summary);
      expect(dashboard.queries.timeseries).toEqual(metrics.queries.timeseries);
      expect(dashboard.queries.summary).toHaveLength(1);
      expect(dashboard.values).toEqual(metrics.values);
    });
  }

  it("following 'Open in Metrics' lands on the same range", async () => {
    search = "range=90d";
    mockFetch(okResponder);
    render(<DashboardPage />);
    const href = screen.getByTestId("dashboard-open-metrics").getAttribute("href")!;
    await waitFor(() => expect(callsTo("/summary")).toHaveLength(1));
    const dashboardSummary = callsTo("/summary")[0].searchParams.toString();
    cleanup();
    search = new URL(href, "http://x").searchParams.toString();
    mockFetch(okResponder);
    render(<MetricsPage />);
    await waitFor(() => expect(callsTo("/summary")).toHaveLength(1));
    expect(callsTo("/summary")[0].searchParams.toString()).toBe(dashboardSummary);
  });
});
