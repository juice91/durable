// P0.18 acceptance tests for the Overview Dashboard, migrated by SL.7 to the Metrics page's Overview
// header (complements overview.test.tsx and lib/metrics.overview.test.ts). Assertions change only
// where SL.7 changes behaviour: no "Open in Metrics" links (the Overview is on Metrics), latency,
// tokens and cost cards are not links (their charts are on the page), all four charts, Top agents
// rows narrow Metrics ("show this agent's metrics"), and the Overview's reading order.
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import React from "react";
import "@testing-library/jest-dom";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import MetricsPage from "./page";
import { ALL_KPIS, BreakdownTable } from "@/components/metrics";
import { breakdownFixture, summaryFixture, timeseriesFixture } from "@/components/metrics/metricsFixtures";
import { parsePageState, pageStateToFilters } from "@/lib/metrics";
import { parseTraceFilters } from "@/lib/traces";
import { legacyTarget, routes } from "@/lib/routes";

/** The KPI cards with a Traces drill-down (one link each); the others are plain cards. */
const LINKED_KPIS = ["runs", "error_rate"] as const;

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

type Responder = (url: URL) => { status: number; body: unknown } | Promise<{ status: number; body: unknown }>;

const SUMMARY = summaryFixture(
  { runs: 12, completed: 6, failed: 3, needs_attention: 2, in_progress: 1, error_rate: 3 / 11, latency_p50_ms: 800, latency_p95_ms: 4200,
    requests: 9, input_tokens: 15000, output_tokens: 2500, cost_usd: "0.0420000000" },
  { runs: 6, completed: 4, failed: 2, needs_attention: 0, error_rate: 2 / 6, latency_p95_ms: 3000, requests: 4,
    input_tokens: 4000, output_tokens: 1000, cost_usd: "0.0100000000" },
);
const mkRun = (id: string, status: string, extra: Record<string, unknown> = {}) => ({
  run_id: id, kind: "agent", agent_id: "a-1", version: 1, status, display_status: status, input: "hi", output: null,
  error: `err ${id}`, heartbeat_at: null, created_at: "2026-11-12T09:00:00Z", updated_at: "2026-11-12T09:00:03Z",
  trace_id: null, conversation_id: null, turn: null, ...extra,
});
const RUNS = { items: [mkRun("run-f1", "failed"), mkRun("run-n1", "needs_attention", { display_status: "needs_attention" })], next_before: null };

const okResponder: Responder = (url) => {
  if (url.pathname === "/api/v1/models") return { status: 200, body: { items: [] } };
  if (url.pathname === "/api/v1/workflows") return { status: 200, body: [] };
  if (url.pathname === "/api/v1/runs") return { status: 200, body: RUNS };
  if (url.pathname.endsWith("/summary")) return { status: 200, body: SUMMARY };
  if (url.pathname.endsWith("/timeseries")) {
    return { status: 200, body: timeseriesFixture([{ runs: 5, failed: 1, cost_usd: "0.02" }, { runs: 3, failed: 0, cost_usd: "0.01" }, { runs: 4, failed: 2, cost_usd: "0.012" }]) };
  }
  if (url.searchParams.get("by") === "agent") {
    return { status: 200, body: breakdownFixture("agent", [{ key: "a-1", label: "Support bot", runs: 12, input_tokens: 15000 }, { key: "a-2", label: "Intake", runs: 3, failed: 2 }]) };
  }
  return { status: 200, body: breakdownFixture("model", []) };
};

let calls: URL[] = [];
function mockFetch(responder: Responder) {
  calls = [];
  vi.stubGlobal("fetch", vi.fn(async (input: string) => {
    const url = new URL(input);
    calls.push(url);
    const { status, body } = await responder(url);
    return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
  }));
}
const callsTo = (suffix: string) => calls.filter((u) => u.pathname.endsWith(suffix));
const dataCalls = () => calls.filter((u) => u.pathname.startsWith("/api/v1/metrics/") || u.pathname === "/api/v1/runs");
const NOW = "2026-11-13T12:00:30Z";
const CUSTOM = "range=custom&from=2026-11-01T00:00:00Z&to=2026-11-05T00:00:00Z";

function setup() {
  beforeEach(() => {
    vi.stubEnv("NEXT_PUBLIC_EAP_DEV_TOKEN", "Bearer test");
    push.mockReset();
    replace.mockReset();
    search = "";
    vi.useFakeTimers({ toFake: ["Date"] });
    vi.setSystemTime(new Date(NOW));
  });
  afterEach(() => {
    cleanup();
    vi.useRealTimers();
    vi.unstubAllGlobals();
    vi.unstubAllEnvs();
  });
}

const enc = (iso: string) => iso.replace(/:/g, "%3A");
const hrefOf = (id: string) => screen.getByTestId(id).getAttribute("href");
const RANGES = {
  "7d": { qs: "", metrics: routes.metrics(), from: "2026-11-06T12:01:00.000Z", to: "2026-11-13T12:01:00.000Z" },
  "30d": { qs: "range=30d", metrics: routes.metrics({ range: "30d" }), from: "2026-10-14T12:01:00.000Z", to: "2026-11-13T12:01:00.000Z" },
  custom: {
    qs: CUSTOM, from: "2026-11-01T00:00:00Z", to: "2026-11-05T00:00:00Z",
    metrics: `${routes.metrics()}?range=custom&from=${enc("2026-11-01T00:00:00Z")}&to=${enc("2026-11-05T00:00:00Z")}`,
  },
} as const;

describe("Overview links (exact hrefs) and round trips", () => {
  setup();
  for (const [name, r] of Object.entries(RANGES)) {
    it(`${name}: KPI cards, agents, failures and view-all`, async () => {
      search = r.qs;
      mockFetch(okResponder);
      render(<MetricsPage />);
      await screen.findAllByTestId("recent-failure");
      await waitFor(() => expect(screen.getByRole("link", { name: "Support bot: show this agent's metrics" })).toBeInTheDocument());
      const range = `from=${enc(r.from)}&to=${enc(r.to)}`;
      expect(hrefOf("kpi-runs")).toBe(`${routes.traces()}?${range}`);
      expect(hrefOf("kpi-error_rate")).toBe(`${routes.traces()}?status=failed&${range}`);
      for (const id of ["latency_p50", "latency_p95", "tokens", "cost"]) expect(hrefOf(`kpi-${id}`)).toBeNull();
      const sep = r.metrics.includes("?") ? "&" : "?";
      expect(screen.getByRole("link", { name: "Support bot: show this agent's metrics" })).toHaveAttribute("href", `${r.metrics}${sep}agent_id=a-1`);
      expect(screen.getByRole("link", { name: "Intake: show this agent's metrics" })).toHaveAttribute("href", `${r.metrics}${sep}agent_id=a-2`);
      const rows = screen.getAllByTestId("recent-failure");
      expect(rows.map((a) => a.getAttribute("href"))).toEqual([routes.trace("run-f1"), routes.trace("run-n1")]);
      expect(hrefOf("recent-failures-all")).toBe(`${routes.traces()}?status=failed%2Cneeds_attention&${range}`);
    });

    it(`${name}: every target page parses the params back to the same filters`, async () => {
      search = r.qs;
      mockFetch(okResponder);
      render(<MetricsPage />);
      await waitFor(() => expect(screen.getByRole("link", { name: "Support bot: show this agent's metrics" })).toBeInTheDocument());
      const dashState = pageStateToFilters(parsePageState(new URLSearchParams(r.qs)), new Date(NOW));
      const parse = (href: string) => new URL(href, "http://x").searchParams;
      // The Top agents target resolves to the page's own range, narrowed to the agent.
      const agent = screen.getByRole("link", { name: "Support bot: show this agent's metrics" }).getAttribute("href")!;
      const fa = pageStateToFilters(parsePageState(parse(agent)), new Date(NOW));
      expect([fa.from, fa.to, fa.agent_id]).toEqual([dashState.from, dashState.to, "a-1"]);
      // Traces targets parse with the Traces page's own parser.
      const runs = parseTraceFilters(parse(hrefOf("kpi-runs")!));
      expect([runs.from, runs.to, runs.status]).toEqual([r.from, r.to, []]);
      const err = parseTraceFilters(parse(hrefOf("kpi-error_rate")!));
      expect([err.from, err.to, err.status]).toEqual([r.from, r.to, ["failed"]]);
      const all = parseTraceFilters(parse(hrefOf("recent-failures-all")!));
      expect([all.from, all.to, all.status]).toEqual([r.from, r.to, ["failed", "needs_attention"]]);
    });
  }
});

describe("Overview trends: chart bar links", () => {
  setup();
  const day = (d: number) => enc(`2026-11-${10 + d}T00:00:00.000Z`);
  const bars = (title: string) =>
    Array.from(within(screen.getByTestId(`chart-${title}`)).getByTestId("chart-plot").querySelectorAll("a[data-bucket]"));

  it("run bars open those runs, failed bars the failed ones, cost bars the bucket's runs; clicks navigate", async () => {
    mockFetch(okResponder);
    render(<MetricsPage />);
    await waitFor(() => expect(bars("Runs and failures")).toHaveLength(6));
    const runs = bars("Runs and failures");
    const byKey = (key: string) => runs.filter((a) => a.getAttribute("data-series") === key).map((a) => a.getAttribute("href"));
    expect(byKey("runs")).toEqual([0, 1, 2].map((i) => `${routes.traces()}?from=${day(i)}&to=${day(i + 1)}`));
    expect(byKey("failed")).toEqual([0, 1, 2].map((i) => `${routes.traces()}?status=failed&from=${day(i)}&to=${day(i + 1)}`));
    await waitFor(() => expect(bars("Cost (USD)")).toHaveLength(3));
    expect(bars("Cost (USD)").map((a) => a.getAttribute("href"))).toEqual([0, 1, 2].map((i) => `${routes.traces()}?from=${day(i)}&to=${day(i + 1)}`));
    // Metrics shows every chart (the dashboard showed runs and cost only).
    expect(screen.getByTestId("chart-Latency")).toBeInTheDocument();
    expect(screen.getByTestId("chart-Tokens")).toBeInTheDocument();
    fireEvent.click(runs.find((a) => a.getAttribute("data-series") === "failed" && a.getAttribute("data-bucket") === "2")!);
    expect(push).toHaveBeenCalledWith(`${routes.traces()}?status=failed&from=${day(2)}&to=${day(3)}`);
  });

  it("bar targets parse back with the Traces page's parser", async () => {
    mockFetch(okResponder);
    render(<MetricsPage />);
    await waitFor(() => expect(bars("Runs and failures")).toHaveLength(6));
    const failed = bars("Runs and failures").find((a) => a.getAttribute("data-series") === "failed" && a.getAttribute("data-bucket") === "1")!;
    const f = parseTraceFilters(new URL(failed.getAttribute("href")!, "http://x").searchParams);
    expect([f.status, f.from, f.to]).toEqual([["failed"], "2026-11-11T00:00:00.000Z", "2026-11-12T00:00:00.000Z"]);
  });
});

const clean = () => expect(document.body.textContent).not.toMatch(/NaN|Infinity|undefined/);

describe("Overview states", () => {
  setup();

  it("an empty tenant: zero cards, empty states, no NaN or Infinity anywhere", async () => {
    mockFetch((url) => {
      if (url.pathname === "/api/v1/runs") return { status: 200, body: { items: [], next_before: null } };
      if (url.pathname.endsWith("/summary")) return { status: 200, body: summaryFixture({}, {}) };
      if (url.pathname.endsWith("/timeseries")) return { status: 200, body: timeseriesFixture([]) };
      if (url.pathname.endsWith("/breakdown")) return { status: 200, body: breakdownFixture("agent", []) };
      return okResponder(url);
    });
    render(<MetricsPage />);
    await waitFor(() => expect(screen.getByTestId("metrics-empty")).toBeInTheDocument());
    await screen.findByTestId("recent-failures-empty");
    expect(screen.getByText("No agent runs in this period")).toBeInTheDocument();
    for (const id of ["runs", "tokens"]) expect(screen.getByTestId(`kpi-${id}-value`)).toHaveTextContent(/^0$/);
    expect(screen.getByTestId("kpi-cost-value")).toHaveTextContent(/^\$0(\.00)?$/);
    expect(screen.getByTestId("kpi-error_rate-value")).toHaveTextContent("—");
    expect(screen.getByTestId("kpi-latency_p95-value")).toHaveTextContent("—");
    expect(screen.getByTestId("recent-failures-all")).toHaveTextContent("View all 0 in Traces");
    clean();
  });

  it("deltas stay finite when the previous period is 0 (or missing) and the current is not", async () => {
    mockFetch((url) =>
      url.pathname.endsWith("/summary")
        ? { status: 200, body: summaryFixture({ runs: 5, completed: 3, failed: 2, error_rate: 0.4, latency_p95_ms: 900, requests: 2, input_tokens: 10, output_tokens: 5, cost_usd: "0.5" }, {}) }
        : okResponder(url),
    );
    render(<MetricsPage />);
    await waitFor(() => expect(screen.getByTestId("kpi-runs-value")).toHaveTextContent("5"));
    for (const id of ALL_KPIS) {
      const delta = screen.queryByTestId(`kpi-${id}-delta`);
      if (delta) expect(delta.textContent).not.toMatch(/NaN|Infinity|null|undefined/);
    }
    expect(screen.getByTestId("kpi-runs-delta")).toHaveTextContent("up from 0");
    clean();
    cleanup();
    mockFetch((url) =>
      url.pathname.endsWith("/summary") ? { status: 200, body: summaryFixture({ runs: 5 }, null) } : okResponder(url),
    );
    render(<MetricsPage />);
    await waitFor(() => expect(screen.getByTestId("kpi-runs-value")).toHaveTextContent("5"));
    clean();
  });

  const sections: [string, string, () => void][] = [
    ["summary", "/summary", () => expect(screen.queryByTestId("kpi-runs")).toBeNull()],
    ["timeseries", "/timeseries", () => expect(within(screen.getByTestId("chart-Runs and failures")).getByRole("alert")).toHaveTextContent("down")],
    ["runs", "/api/v1/runs", () => expect(within(screen.getByTestId("recent-failures")).getByRole("alert")).toHaveTextContent("down")],
  ];
  for (const [name, path, check] of sections) {
    it(`the ${name} failing leaves the other sections rendered`, async () => {
      mockFetch((url) => (url.pathname.endsWith(path) ? { status: 500, body: { detail: `${name} down` } } : okResponder(url)));
      render(<MetricsPage />);
      await waitFor(() => expect(screen.getAllByRole("alert").length).toBeGreaterThan(0));
      await waitFor(check);
      if (name !== "summary") expect(screen.getByTestId("kpi-runs-value")).toHaveTextContent("12");
      if (name !== "runs") await screen.findAllByTestId("recent-failure");
      if (name !== "timeseries") await waitFor(() => expect(screen.getAllByTestId("chart-plot").length).toBeGreaterThan(0));
      await waitFor(() => expect(screen.getByRole("link", { name: "Support bot: show this agent's metrics" })).toBeInTheDocument());
    });
  }

  it("the top agents failing leaves KPIs, charts and failures rendered", async () => {
    mockFetch((url) => (url.pathname.endsWith("/breakdown") ? { status: 500, body: { detail: "agents down" } } : okResponder(url)));
    render(<MetricsPage />);
    await screen.findAllByTestId("recent-failure");
    // Both breakdowns (Top agents and By model) fail; each shows the error.
    await waitFor(() => expect(screen.getAllByText("agents down")).toHaveLength(2));
    expect(screen.getByTestId("kpi-runs-value")).toHaveTextContent("12");
    expect(screen.getAllByTestId("chart-plot").length).toBeGreaterThan(0);
  });

  it("an invalid custom range is refused with no fetch, also on Refresh", async () => {
    search = "range=custom&from=2026-11-05T00:00:00Z&to=2026-11-01T00:00:00Z";
    mockFetch(okResponder);
    render(<MetricsPage />);
    expect(screen.getByRole("alert")).toHaveTextContent("from must be before to");
    expect(screen.queryByTestId("kpi-runs")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Refresh" }));
    await act(async () => { await Promise.resolve(); });
    expect(dataCalls()).toEqual([]);
  });
});

describe("Overview behaviour", () => {
  setup();

  it("an older response arriving last does not overwrite a newer range", async () => {
    let releaseOld: () => void = () => undefined;
    const gate = new Promise<void>((r) => { releaseOld = r; });
    const body = (runs: number) => summaryFixture({ runs, completed: runs }, {});
    mockFetch(async (url) => {
      if (url.pathname.endsWith("/summary")) {
        const from = Date.parse(url.searchParams.get("from")!);
        const days = Math.round((Date.parse(url.searchParams.get("to")!) - from) / 86_400_000);
        if (days === 7) { await gate; return { status: 200, body: body(111) }; }
        return { status: 200, body: body(222) };
      }
      return okResponder(url);
    });
    const { rerender } = render(<MetricsPage />);
    await waitFor(() => expect(callsTo("/summary")).toHaveLength(1));
    search = "range=30d";
    rerender(<MetricsPage />);
    await waitFor(() => expect(screen.getByTestId("kpi-runs-value")).toHaveTextContent("222"));
    await act(async () => { releaseOld(); await new Promise((r) => setTimeout(r, 20)); });
    expect(screen.getByTestId("kpi-runs-value")).toHaveTextContent("222");
    expect(screen.queryByText("111")).toBeNull();
    expect(screen.getByRole("link", { name: "Support bot: show this agent's metrics" })).toHaveAttribute("href", routes.metrics({ range: "30d", agent_id: "a-1" }));
  });

  it("a stale error from the old range does not replace the new data", async () => {
    let failOld: () => void = () => undefined;
    const gate = new Promise<void>((r) => { failOld = r; });
    mockFetch(async (url) => {
      if (url.pathname.endsWith("/summary") && Date.parse(url.searchParams.get("to")!) - Date.parse(url.searchParams.get("from")!) < 8 * 86_400_000) {
        await gate;
        return { status: 500, body: { detail: "old range failed" } };
      }
      return okResponder(url);
    });
    const { rerender } = render(<MetricsPage />);
    await waitFor(() => expect(callsTo("/summary")).toHaveLength(1));
    search = "range=30d";
    rerender(<MetricsPage />);
    await waitFor(() => expect(screen.getByTestId("kpi-runs-value")).toHaveTextContent("12"));
    await act(async () => { failOld(); await new Promise((r) => setTimeout(r, 20)); });
    expect(screen.queryByText("old range failed")).toBeNull();
    expect(screen.getByTestId("kpi-runs-value")).toHaveTextContent("12");
  });

  it("Refresh re-fetches every section", async () => {
    mockFetch(okResponder);
    render(<MetricsPage />);
    await screen.findAllByTestId("recent-failure");
    // Two breakdowns per load: Top agents and By model.
    await waitFor(() => expect(callsTo("/breakdown")).toHaveLength(2));
    fireEvent.click(screen.getByRole("button", { name: "Refresh" }));
    await waitFor(() => expect(callsTo("/summary")).toHaveLength(2));
    await waitFor(() => expect(callsTo("/timeseries")).toHaveLength(2));
    await waitFor(() => expect(callsTo("/breakdown")).toHaveLength(4));
    await waitFor(() => expect(calls.filter((u) => u.pathname === "/api/v1/runs")).toHaveLength(2));
  });

  async function advance(ms: number) {
    await act(async () => { vi.advanceTimersByTime(ms); });
  }

  it("a preset range re-fetches every 5 minutes with a moved window", async () => {
    vi.useRealTimers();
    vi.useFakeTimers({ toFake: ["Date", "setInterval", "clearInterval"] });
    vi.setSystemTime(new Date(NOW));
    mockFetch(okResponder);
    render(<MetricsPage />);
    await waitFor(() => expect(callsTo("/summary")).toHaveLength(1));
    const first = callsTo("/summary")[0].searchParams.get("to");
    await advance(5 * 60_000);
    await waitFor(() => expect(callsTo("/summary")).toHaveLength(2));
    expect(callsTo("/summary")[1].searchParams.get("to")).not.toBe(first);
    await waitFor(() => expect(callsTo("/breakdown")).toHaveLength(4));
    await waitFor(() => expect(callsTo("/timeseries")).toHaveLength(2));
    await waitFor(() => expect(calls.filter((u) => u.pathname === "/api/v1/runs")).toHaveLength(2));
    const runsCalls = calls.filter((u) => u.pathname === "/api/v1/runs");
    expect(runsCalls[1].searchParams.get("to")).toBe(callsTo("/summary")[1].searchParams.get("to"));
  });

  it("a custom range never auto-refreshes", async () => {
    vi.useRealTimers();
    vi.useFakeTimers({ toFake: ["Date", "setInterval", "clearInterval"] });
    vi.setSystemTime(new Date(NOW));
    search = CUSTOM;
    mockFetch(okResponder);
    render(<MetricsPage />);
    await waitFor(() => expect(callsTo("/summary")).toHaveLength(1));
    await advance(30 * 60_000);
    expect(dataCalls().filter((u) => u.pathname.endsWith("/summary"))).toHaveLength(1);
    expect(callsTo("/timeseries")).toHaveLength(1);
  });
});

const TABBABLE = "a[href], button:not([disabled]), select:not([disabled]), input:not([disabled]), [tabindex]";
const tabbables = () =>
  Array.from(document.body.querySelectorAll<HTMLElement>(TABBABLE)).filter((el) => el.getAttribute("tabindex") !== "-1");

describe("Overview accessibility", () => {
  setup();

  async function loaded() {
    mockFetch(okResponder);
    render(<MetricsPage />);
    await screen.findAllByTestId("recent-failure");
    await waitFor(() => expect(screen.getAllByTestId("chart-plot").length).toBeGreaterThanOrEqual(2));
    await waitFor(() => expect(screen.getByRole("link", { name: "Intake: show this agent's metrics" })).toBeInTheDocument());
  }

  it("one tab stop per card, per failure row, per agent, and one group per chart (bars are not stops)", async () => {
    await loaded();
    const stops = tabbables();
    for (const id of ALL_KPIS) {
      expect(stops.filter((el) => el === screen.getByTestId(`kpi-${id}`))).toHaveLength((LINKED_KPIS as readonly string[]).includes(id) ? 1 : 0);
    }
    expect(stops.filter((el) => el.matches("[data-testid=recent-failure]"))).toHaveLength(2);
    for (const a of screen.getAllByTestId("chart-plot")) {
      expect(stops.filter((el) => el === a)).toHaveLength(1);
      expect(a).toHaveAttribute("role", "group");
      expect(stops.filter((el) => a.contains(el) && el !== a)).toEqual([]);
    }
    expect(document.querySelectorAll("a[data-bucket]:not([tabindex='-1'])")).toHaveLength(0);
    // A row without failures has one stop (its name); a row with failures also has the failed link.
    const supportRow = screen.getByTestId("breakdown-row-agent-a-1");
    expect(stops.filter((el) => supportRow.contains(el) && el.tagName === "A")).toHaveLength(1);
    const intakeRow = screen.getByTestId("breakdown-row-agent-a-2");
    const intakeStops = stops.filter((el) => intakeRow.contains(el) && el.tagName === "A");
    expect(intakeStops).toEqual([
      within(intakeRow).getByRole("link", { name: "Intake: show this agent's metrics" }),
      within(intakeRow).getByRole("link", { name: "Intake: open 2 failed runs" }),
    ]);
    const failedHref = new URL(intakeStops[1].getAttribute("href")!, "http://x");
    expect(failedHref.pathname).toBe(routes.traces());
    expect(failedHref.searchParams.get("agent_id")).toBe("a-2");
    expect(failedHref.searchParams.get("status")).toBe("failed");
    // Nothing inside a KPI card or failure row is a second stop (no nested links or buttons).
    for (const el of [...ALL_KPIS.map((id) => screen.getByTestId(`kpi-${id}`)), ...screen.getAllByTestId("recent-failure")]) {
      expect(el.querySelectorAll(TABBABLE)).toHaveLength(0);
    }
  });

  it("the tab order follows the reading order: filters, KPIs, needs attention, top agents, trends", async () => {
    await loaded();
    const order = (el: Element) => tabbables().indexOf(el as HTMLElement);
    const seq = [
      screen.getByLabelText("Date range"),
      screen.getByTestId("kpi-runs"),
      screen.getByTestId("kpi-error_rate"),
      screen.getByTestId("recent-failures-all"),
      screen.getAllByTestId("recent-failure")[0],
      screen.getByRole("link", { name: "Support bot: show this agent's metrics" }),
      screen.getAllByTestId("chart-plot")[0],
    ].map(order);
    expect(seq.every((n) => n >= 0)).toBe(true);
    expect([...seq].sort((a, b) => a - b)).toEqual(seq);
  });

  it("controls and links have accessible names", async () => {
    await loaded();
    expect(screen.getByRole("heading", { level: 1, name: "Metrics" })).toBeInTheDocument();
    expect(screen.getByRole("region", { name: "Overview" })).toBe(screen.getByTestId("metrics-overview"));
    expect(screen.getByRole("region", { name: "Needs attention" })).toBe(screen.getByTestId("recent-failures"));
    expect(screen.getByRole("region", { name: "Top agents" })).toBeInTheDocument();
    expect(screen.getByRole("region", { name: "Trends" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "View all 5 in Traces" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /^Runs\s*12/ })).toBe(screen.getByTestId("kpi-runs"));
    expect(screen.getByRole("link", { name: /^Error rate\s*27\.3%/ })).toBe(screen.getByTestId("kpi-error_rate"));
    expect(screen.getByTestId("kpi-cost")).toHaveTextContent(/^Cost \(USD\)\s*\$0\.04/);
    expect(screen.getByTestId("kpi-cost").tagName).toBe("DIV");
    const failure = screen.getAllByTestId("recent-failure")[1];
    expect(failure).toHaveAccessibleName(/Support bot.*needs attention.*err run-n1/i);
    expect(screen.getByRole("group", { name: /^Runs and failures chart\./ })).toBeInTheDocument();
    expect(screen.getByRole("group", { name: /^Cost \(USD\) chart\./ })).toBeInTheDocument();
    for (const el of screen.getAllByRole("link")) expect(el).toHaveAccessibleName();
    for (const el of screen.getAllByRole("button")) expect(el).toHaveAccessibleName();
    for (const el of screen.getAllByRole("combobox")) expect(el).toHaveAccessibleName();
  });
});

describe("Old dashboard links and Metrics parity with a busy previous period and needs_attention runs", () => {
  setup();
  const BUSY = summaryFixture(
    { runs: 20, completed: 10, failed: 4, needs_attention: 3, in_progress: 2, blocked: 1, error_rate: 4 / 17, latency_p50_ms: 900,
      latency_p95_ms: 6100, requests: 30, input_tokens: 120000, output_tokens: 30000, cost_usd: "1.2345678900", unpriced_requests: 2 },
    { runs: 15, completed: 8, failed: 5, needs_attention: 2, in_progress: 0, error_rate: 5 / 13, latency_p95_ms: 7000, requests: 25,
      input_tokens: 90000, output_tokens: 20000, cost_usd: "2.5000000000", unpriced_requests: 0 },
  );
  const busyResponder: Responder = (url) => (url.pathname.endsWith("/summary") ? { status: 200, body: BUSY } : okResponder(url));

  async function read(page: React.ReactElement) {
    mockFetch(busyResponder);
    render(page);
    await waitFor(() => expect(screen.getByTestId("kpi-runs-value")).toHaveTextContent("20"));
    const cards = Object.fromEntries(ALL_KPIS.map((id) => [id, screen.getByTestId(`kpi-${id}`).textContent]));
    const caption = screen.getByTestId("kpi-comparison").textContent;
    const queries = [callsTo("/summary"), callsTo("/timeseries")].map((l) => l.map((u) => u.searchParams.toString()));
    cleanup();
    return { cards, caption, queries };
  }

  for (const qs of ["", "range=30d", CUSTOM]) {
    it(`an old dashboard link lands on the same cards (value, delta, hint) and comparison caption (${qs || "default"})`, async () => {
      // A bare "/" now opens Agents, so the default range is checked through its explicit form.
      const target = new URL(legacyTarget("/", new URLSearchParams(qs || "range=7d"))!, "http://x");
      expect(target.pathname).toBe(routes.metrics());
      search = target.searchParams.toString();
      const dashboard = await read(<MetricsPage />);
      search = qs;
      const metrics = await read(<MetricsPage />);
      expect(dashboard.cards).toEqual(metrics.cards);
      expect(dashboard.caption).toBe(metrics.caption);
      expect(dashboard.queries).toEqual(metrics.queries);
      expect(dashboard.cards.error_rate).toMatch(/23\.5%/);
      expect(dashboard.cards.cost).toMatch(/\$1\.23/);
      expect(dashboard.cards.cost).toMatch(/down 5[0-9]%?/);
    });
  }

  it("the failures count equals failed + needs_attention of the same summary", async () => {
    mockFetch(busyResponder);
    render(<MetricsPage />);
    await screen.findAllByTestId("recent-failure");
    await waitFor(() => expect(screen.getByTestId("recent-failures-all")).toHaveTextContent("View all 7 in Traces"));
    expect(screen.getByTestId("kpi-error_rate-delta")).toHaveTextContent(/down/);
    // The same page's error-rate card names the needs-attention runs the count includes.
    expect(screen.getByTestId("kpi-runs-value")).toHaveTextContent("20");
    expect(document.body.textContent).toMatch(/3 needs attention|needs attention:? 3|3 need attention/i);
  });
});

describe("BreakdownTable without rowLink (unchanged default)", () => {
  setup();
  const items = breakdownFixture("agent", [{ key: "a-1", label: "Support bot", runs: 4 }]).items;
  const filters = { from: "2026-11-10T00:00:00Z", to: "2026-11-13T00:00:00Z", agent_id: null, model: null, provider: null, kind: null };

  it("links to the Traces list filtered to the row, named 'open matching runs'", () => {
    const onNavigate = vi.fn();
    render(<BreakdownTable title="By agent" by="agent" items={items} filters={filters} sort="runs" onSortChange={() => undefined} onNavigate={onNavigate} />);
    const link = screen.getByRole("link", { name: "Support bot: open matching runs" });
    const url = new URL(link.getAttribute("href")!, "http://x");
    expect(url.pathname).toBe(routes.traces());
    expect(url.searchParams.get("agent_id")).toBe("a-1");
    expect(url.searchParams.get("from")).toBe(filters.from);
    fireEvent.click(within(screen.getByTestId("breakdown-row-agent-a-1")).getByText("4"));
    expect(onNavigate).toHaveBeenCalledWith(link.getAttribute("href"));
  });

  it("rowLink replaces both the link and the row click target", () => {
    const onNavigate = vi.fn();
    render(
      <BreakdownTable title="By agent" by="agent" items={items} filters={filters} sort="runs" onSortChange={() => undefined} onNavigate={onNavigate}
        rowLink={(item, name) => ({ href: `/x/${item.key}`, label: `${name}: custom` })} />,
    );
    expect(screen.getByRole("link", { name: "Support bot: custom" })).toHaveAttribute("href", "/x/a-1");
    expect(screen.queryByRole("link", { name: /open matching runs/ })).toBeNull();
    fireEvent.click(within(screen.getByTestId("breakdown-row-agent-a-1")).getByText("4"));
    expect(onNavigate).toHaveBeenCalledWith("/x/a-1");
  });
});
