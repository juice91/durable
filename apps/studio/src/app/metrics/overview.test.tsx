// The Metrics Overview header (SL.7): the former P0.18 dashboard page tests (app/page.test.tsx),
// migrated to the Metrics page with the same numbers, links and empty states.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import React from "react";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import MetricsPage from "./page";
import RootPage from "../page";
import { breakdownFixture, summaryFixture, timeseriesFixture } from "@/components/metrics/metricsFixtures";
import { ALL_KPIS } from "@/components/metrics";
import { legacyTarget, routes } from "@/lib/routes";

const push = vi.fn();
const replace = vi.fn();
let search = "";

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push, replace }),
  useSearchParams: () => new URLSearchParams(search),
  redirect: (url: string) => {
    throw new Error(`REDIRECT:${url}`);
  },
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

describe("Metrics Overview", () => {
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

  it("an operation filter: Needs attention lists no runs (runs have no operation), says why, and links without a count", async () => {
    search = "operation=observer";
    mockFetch(okResponder);
    render(<MetricsPage />);
    const panel = await screen.findByTestId("recent-failures");
    await waitFor(() => expect(callsTo("/summary").length).toBeGreaterThan(0));
    expect(within(panel).getByTestId("recent-failures-operation")).toHaveTextContent("while the Observer filter is on");
    expect(within(panel).queryAllByTestId("recent-failure")).toHaveLength(0);
    expect(within(panel).getByTestId("recent-failures-all")).toHaveTextContent("View failed runs in Traces");
    expect(within(panel).getByTestId("recent-failures-all")).not.toHaveTextContent(/View all \d/);
    // No runs request for the panel while it cannot list them.
    expect(calls.filter((u) => u.pathname === "/api/v1/runs")).toHaveLength(0);
  });

  it("shows KPI cards with deltas, trends, top agents and the runs that need attention from the API", async () => {
    mockFetch(okResponder);
    render(<MetricsPage />);
    const overview = screen.getByTestId("metrics-overview");
    expect(within(overview).getByRole("heading", { level: 2, name: "Overview" })).toBeInTheDocument();
    await waitFor(() => expect(screen.getByTestId("kpi-runs-value")).toHaveTextContent("12"));
    expect(screen.getByTestId("kpi-error_rate-value")).toHaveTextContent("27.3%");
    expect(screen.getByTestId("kpi-latency_p95-value")).toHaveTextContent("4.2 s");
    expect(screen.getByTestId("kpi-tokens-value")).toHaveTextContent("17,500");
    expect(screen.getByTestId("kpi-cost-value")).toHaveTextContent("$0.042");
    expect(screen.getByTestId("kpi-runs-delta")).toHaveTextContent("up 100% from 6");
    // Metrics keeps its p50 card and every chart (the dashboard showed a subset).
    expect(screen.getByTestId("kpi-latency_p50-value")).toHaveTextContent("800 ms");
    expect(within(overview).getByTestId("kpi-runs")).toBeInTheDocument();
    expect(screen.getByText("Runs and failures")).toBeInTheDocument();
    expect(screen.getAllByText("Cost (USD)").length).toBeGreaterThan(0);
    expect(screen.getByText("Latency")).toBeInTheDocument();
    await waitFor(() => expect(screen.getByTestId("recent-failure")).toHaveAttribute("href", routes.trace("run-failed-1")));
    expect(within(overview).getByTestId("recent-failures")).toBeInTheDocument();
    expect(screen.getByTestId("recent-failure")).toHaveTextContent("Support bot");
    expect(screen.getByTestId("recent-failure")).toHaveTextContent("model_error: upstream 500");
    expect(screen.getByTestId("recent-failures-all")).toHaveTextContent("View all 4 in Traces");
    await waitFor(() => expect(within(overview).getByRole("link", { name: "Support bot: show this agent's metrics" })).toBeInTheDocument());
  });

  it("the Overview follows the page's one filter state: top 5 agents by tokens, attention runs of the same window", async () => {
    vi.useFakeTimers({ toFake: ["Date"] });
    vi.setSystemTime(new Date("2026-11-13T12:00:30Z"));
    search = "range=24h&agent_id=a-9";
    mockFetch(okResponder);
    render(<MetricsPage />);
    await waitFor(() => expect(callsTo("/api/v1/runs")).toHaveLength(1));
    const summary = callsTo("/summary")[0];
    expect(summary.searchParams.get("from")).toBe("2026-11-12T12:01:00.000Z");
    expect(summary.searchParams.get("to")).toBe("2026-11-13T12:01:00.000Z");
    expect(summary.searchParams.get("agent_id")).toBe("a-9");
    expect(summary.searchParams.get("compare")).toBe("previous");
    await waitFor(() => expect(callsTo("/breakdown")).toHaveLength(2));
    const breakdown = callsTo("/breakdown").find((u) => u.searchParams.get("by") === "agent")!;
    expect([breakdown.searchParams.get("by"), breakdown.searchParams.get("sort"), breakdown.searchParams.get("limit")]).toEqual(["agent", "tokens", "5"]);
    expect(breakdown.searchParams.get("agent_id")).toBe("a-9");
    const runs = callsTo("/api/v1/runs")[0];
    expect(runs.searchParams.get("status")).toBe("failed,needs_attention");
    expect(runs.searchParams.get("from")).toBe("2026-11-12T12:01:00.000Z");
    expect(runs.searchParams.get("to")).toBe("2026-11-13T12:01:00.000Z");
    expect(runs.searchParams.get("agent_id")).toBe("a-9");
    expect(runs.searchParams.get("limit")).toBe("5");
  });

  it("every Overview element links to Traces or narrows Metrics with the page's filters", async () => {
    vi.useFakeTimers({ toFake: ["Date"] });
    vi.setSystemTime(new Date("2026-11-13T12:00:30Z"));
    search = "range=30d";
    mockFetch(okResponder);
    render(<MetricsPage />);
    await waitFor(() => expect(screen.getByTestId("kpi-runs")).toHaveAttribute("href"));
    const traces = new URL(screen.getByTestId("kpi-runs").getAttribute("href")!, "http://x");
    expect(traces.pathname).toBe(routes.traces());
    expect(traces.searchParams.get("from")).toBe("2026-10-14T12:01:00.000Z");
    expect(traces.searchParams.get("to")).toBe("2026-11-13T12:01:00.000Z");
    expect(screen.getByTestId("kpi-error_rate").getAttribute("href")).toContain("status=failed");
    // Latency, tokens and cost have no Traces view; on Metrics their charts are right below.
    for (const id of ["latency_p95", "tokens", "cost"]) expect(screen.getByTestId(`kpi-${id}`).tagName).toBe("DIV");
    await waitFor(() =>
      expect(screen.getByRole("link", { name: "Support bot: show this agent's metrics" })).toHaveAttribute("href", routes.metrics({ range: "30d", agent_id: "a-1" })),
    );
    fireEvent.click(within(screen.getByTestId("breakdown-row-agent-a-1")).getByText("12"));
    expect(push).toHaveBeenCalledWith(routes.metrics({ range: "30d", agent_id: "a-1" }));
    const all = new URL(screen.getByTestId("recent-failures-all").getAttribute("href")!, "http://x");
    expect(all.pathname).toBe(routes.traces());
    expect(all.searchParams.get("status")).toBe("failed,needs_attention");
    expect(all.searchParams.get("from")).toBe("2026-10-14T12:01:00.000Z");
  });

  it("'Show 10' re-fetches Top agents with limit 10 (agents ranked 6-10 stay reachable)", async () => {
    const many = breakdownFixture("agent", Array.from({ length: 5 }, (_, i) => ({ key: `a-${i + 1}`, label: `Agent ${i + 1}`, runs: 10 - i })));
    mockFetch((url) => (url.searchParams.get("by") === "agent" ? { status: 200, body: many } : okResponder(url)));
    render(<MetricsPage />);
    const toggle = await screen.findByTestId("top-agents-limit");
    fireEvent.click(toggle);
    await waitFor(() => expect(callsTo("/breakdown").filter((u) => u.searchParams.get("by") === "agent").map((u) => u.searchParams.get("limit"))).toEqual(["5", "10"]));
  });

  it("changing the date range updates the Metrics URL; the dimension filters are on the same bar", async () => {
    mockFetch(okResponder);
    const { rerender } = render(<MetricsPage />);
    fireEvent.change(screen.getByLabelText("Date range"), { target: { value: "30d" } });
    expect(replace).toHaveBeenCalledWith(routes.metrics({ range: "30d" }), { scroll: false });
    // The URL is mocked: show the page as it would be on 30d, then pick the default again.
    search = "range=30d";
    rerender(<MetricsPage />);
    fireEvent.change(screen.getByLabelText("Date range"), { target: { value: "7d" } });
    expect(replace).toHaveBeenLastCalledWith(routes.metrics(), { scroll: false });
    expect(screen.getByLabelText("Agent")).toBeInTheDocument();
    expect(screen.getByLabelText("Interval")).toBeInTheDocument();
  });

  it("an empty tenant gets zero cards, empty panels and links to start a run", async () => {
    mockFetch(emptyResponder);
    render(<MetricsPage />);
    await waitFor(() => expect(screen.getByTestId("metrics-empty")).toBeInTheDocument());
    expect(screen.getByTestId("kpi-runs-value")).toHaveTextContent("0");
    expect(screen.getByTestId("kpi-error_rate-value")).toHaveTextContent("—");
    const empty = screen.getByTestId("metrics-empty");
    expect(empty).toHaveTextContent("No runs or model requests in this period. Widen the date range, or start a run:");
    expect(within(empty).getByRole("link", { name: "Agents" })).toHaveAttribute("href", routes.agents());
    expect(within(empty).getByRole("link", { name: "Workflows" })).toHaveAttribute("href", routes.workflows());
    await waitFor(() => expect(screen.getByTestId("recent-failures-empty")).toBeInTheDocument());
    await waitFor(() => expect(screen.getByText("No agent runs in this period")).toBeInTheDocument());
  });

  it("with a filter set, the empty state says to clear it (no start-a-run links)", async () => {
    search = "agent_id=a-1";
    mockFetch(emptyResponder);
    render(<MetricsPage />);
    const empty = await screen.findByTestId("metrics-empty");
    expect(empty).toHaveTextContent("No runs or model requests match these filters in this period. Widen the date range or clear the filters.");
    expect(within(empty).queryByRole("link")).toBeNull();
  });

  it("shows the API error for the summary and for each panel", async () => {
    mockFetch((url) =>
      url.pathname.startsWith("/api/v1/metrics/") || url.pathname === "/api/v1/runs"
        ? { status: 500, body: { detail: "database unavailable" } }
        : okResponder(url),
    );
    render(<MetricsPage />);
    await waitFor(() => expect(screen.getAllByRole("alert").length).toBeGreaterThanOrEqual(3));
    for (const alert of screen.getAllByRole("alert")) expect(alert).toHaveTextContent("database unavailable");
    expect(within(screen.getByTestId("recent-failures")).getByRole("alert")).toBeInTheDocument();
  });

  it("an invalid custom range is refused without calling the API", async () => {
    search = "range=custom&from=2026-01-01T00:00:00Z&to=2026-11-01T00:00:00Z";
    mockFetch(okResponder);
    render(<MetricsPage />);
    expect(screen.getByRole("alert")).toHaveTextContent("the range must be at most 90 days");
    await Promise.resolve();
    expect(calls.filter((u) => u.pathname.startsWith("/api/v1/metrics/") || u.pathname === "/api/v1/runs")).toEqual([]);
  });
});

describe("Old dashboard links and Metrics parity (P0.18 acceptance, restated for SL.7)", () => {
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

  async function renderAndRead(qs: string) {
    search = qs;
    mockFetch(okResponder);
    render(<MetricsPage />);
    await waitFor(() => expect(screen.getByTestId("kpi-runs-value")).toHaveTextContent("12"));
    await waitFor(() => expect(callsTo("/api/v1/runs")).toHaveLength(1));
    const values = Object.fromEntries(
      ALL_KPIS.map((id) => [id, {
        value: screen.getByTestId(`kpi-${id}-value`).textContent,
        delta: screen.queryByTestId(`kpi-${id}-delta`)?.textContent ?? null,
      }]),
    );
    const query = (suffix: string) => callsTo(suffix).map((u) => u.searchParams.toString());
    const window = (u: URL) => [u.searchParams.get("from"), u.searchParams.get("to")];
    const queries = { summary: query("/summary"), timeseries: query("/timeseries") };
    const windows = [...callsTo("/summary"), ...callsTo("/timeseries"), ...callsTo("/breakdown"), ...callsTo("/api/v1/runs")].map(window);
    cleanup();
    return { values, queries, windows };
  }

  for (const qs of ["", "range=24h", "range=90d", "range=custom&from=2026-11-01T00:00:00Z&to=2026-11-05T00:00:00Z"]) {
    it(`an old dashboard link (${qs || "default"}) lands on Metrics with the same queries and KPI numbers`, async () => {
      const target = legacyTarget("/", new URLSearchParams(qs))!;
      // A bare "/" now opens Agents; a dashboard link with a range opens Metrics on that range.
      if (!qs) {
        expect(target).toBe(routes.agents());
        return;
      }
      expect(new URL(target, "http://x").pathname).toBe(routes.metrics());
      const viaLegacy = await renderAndRead(new URL(target, "http://x").searchParams.toString());
      const direct = await renderAndRead(qs);
      expect(viaLegacy.queries).toEqual(direct.queries);
      expect(viaLegacy.queries.summary).toHaveLength(1);
      expect(viaLegacy.values).toEqual(direct.values);
    });

    it(`one window for the whole page (${qs || "default"}): KPIs, charts, Top agents and Needs attention`, async () => {
      const { windows } = await renderAndRead(qs);
      expect(windows.length).toBeGreaterThanOrEqual(5);
      for (const w of windows) expect(w).toEqual(windows[0]);
    });
  }

  it("the redirect page sends a dashboard link to Metrics and a bare / to Agents", async () => {
    await expect(RootPage({ searchParams: Promise.resolve({ range: "90d" }) })).rejects.toThrow(`REDIRECT:${routes.metrics({ range: "90d" })}`);
    await expect(RootPage({ searchParams: Promise.resolve({}) })).rejects.toThrow(`REDIRECT:${routes.agents()}`);
  });
});
