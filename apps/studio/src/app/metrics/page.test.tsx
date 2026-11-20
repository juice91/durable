import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import React from "react";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import MetricsPage from "./page";
import { breakdownFixture, summaryFixture, timeseriesFixture } from "@/components/metrics/metricsFixtures";
import { routes } from "@/lib/routes";
import { optionsOf } from "@/test/select";

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

const MODELS = {
  items: [
    { id: "openai:gpt-4o", provider: "openai", name: "gpt-4o", wildcard: false, source: "allowlist", description: null },
    { id: "litellm:*", provider: "litellm", name: "*", wildcard: true, source: "allowlist", description: null },
    { id: "test:echo", provider: "test", name: "echo", wildcard: false, source: "scripted", description: "Echo" },
  ],
};

const okResponder: Responder = (url) => {
  if (url.pathname === "/api/v1/models") return { status: 200, body: MODELS };
  // The Overview's Needs attention list and its run names (SL.7).
  if (url.pathname === "/api/v1/runs") return { status: 200, body: { items: [], next_before: null } };
  if (url.pathname === "/api/v1/workflows") return { status: 200, body: [] };
  if (url.pathname.endsWith("/summary")) {
    return { status: 200, body: summaryFixture({ runs: 6, completed: 3, failed: 3, error_rate: 0.5, requests: 2, cost_usd: "0.0100000000" }, { runs: 3 }) };
  }
  if (url.pathname.endsWith("/timeseries")) {
    return { status: 200, body: timeseriesFixture([{ runs: 4, failed: 1 }, { runs: 0 }, { runs: 2, failed: 2 }]) };
  }
  if (url.searchParams.get("by") === "agent") {
    return { status: 200, body: breakdownFixture("agent", [{ key: "a-1", label: "Support bot", runs: 6 }]) };
  }
  return { status: 200, body: breakdownFixture("model", [{ key: "anthropic:legacy", runs: 6, requests: 2 }]) };
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

const metricCalls = () => calls.filter((u) => u.pathname.startsWith("/api/v1/metrics/"));

describe("MetricsPage", () => {
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

  const summaryTo = () => metricCalls().filter((u) => u.pathname.endsWith("/summary")).map((u) => u.searchParams.get("to"));

  it("model and provider options come from GET /models merged with the models seen in breakdowns", async () => {
    mockFetch(okResponder);
    render(<MetricsPage />);
    const model = screen.getByLabelText("Model");
    await waitFor(() =>
      expect(optionsOf(model).map((o) => o.textContent)).toEqual([
        "All models", "anthropic:legacy", "openai:gpt-4o", "test:echo",
      ]),
    );
    expect(optionsOf(screen.getByLabelText("Provider")).map((o) => o.textContent)).toEqual([
      "All providers", "anthropic", "litellm", "openai", "test",
    ]);
  });

  it("preset ranges end at the current time when the range changes and on Refresh, not at mount time", async () => {
    vi.useFakeTimers({ toFake: ["Date"] });
    vi.setSystemTime(new Date("2026-11-13T10:00:00Z"));
    search = "range=24h";
    mockFetch(okResponder);
    const { rerender } = render(<MetricsPage />);
    await waitFor(() => expect(summaryTo()).toEqual(["2026-11-13T10:00:00.000Z"]));

    vi.setSystemTime(new Date("2026-11-13T10:20:00Z"));
    search = "range=7d";
    rerender(<MetricsPage />);
    await waitFor(() => expect(summaryTo()).toEqual(["2026-11-13T10:00:00.000Z", "2026-11-13T10:20:00.000Z"]));
    const latest = metricCalls().filter((u) => u.pathname.endsWith("/summary")).at(-1)!;
    expect(latest.searchParams.get("from")).toBe("2026-11-06T10:20:00.000Z");

    vi.setSystemTime(new Date("2026-11-13T10:45:00Z"));
    fireEvent.click(screen.getByRole("button", { name: "Refresh" }));
    await waitFor(() => expect(summaryTo().at(-1)).toBe("2026-11-13T10:45:00.000Z"));
  });

  it("preset ranges move forward on a 5 minute timer", async () => {
    vi.useFakeTimers({ toFake: ["Date", "setInterval", "clearInterval"] });
    vi.setSystemTime(new Date("2026-11-13T10:00:00Z"));
    mockFetch(okResponder);
    render(<MetricsPage />);
    await waitFor(() => expect(summaryTo()).toEqual(["2026-11-13T10:00:00.000Z"]));
    // advanceTimersByTime also moves the faked Date forward by 5 minutes.
    await act(async () => {
      vi.advanceTimersByTime(5 * 60_000);
    });
    await waitFor(() => expect(summaryTo().at(-1)).toBe("2026-11-13T10:05:00.000Z"));
  });

  it("a custom range is fixed: Refresh re-fetches the same window", async () => {
    search = "range=custom&from=2026-11-01T00:00:00.000Z&to=2026-11-02T00:00:00.000Z";
    mockFetch(okResponder);
    render(<MetricsPage />);
    await waitFor(() => expect(summaryTo()).toHaveLength(1));
    fireEvent.click(screen.getByRole("button", { name: "Refresh" }));
    await waitFor(() => expect(summaryTo()).toEqual(["2026-11-02T00:00:00.000Z", "2026-11-02T00:00:00.000Z"]));
  });

  it("loads summary with comparison, timeseries and both breakdowns, and shows KPIs", async () => {
    mockFetch(okResponder);
    render(<MetricsPage />);
    expect(await screen.findByTestId("kpi-runs-value")).toHaveTextContent("6");
    expect(screen.getByTestId("kpi-runs-delta")).toHaveTextContent("up 100% from 3");
    expect(screen.getByTestId("kpi-error_rate-value")).toHaveTextContent("50%");
    // The agent breakdown is the Overview's Top agents (SL.7): a row narrows Metrics to the agent.
    expect(await screen.findByRole("link", { name: "Support bot: show this agent's metrics" })).toHaveAttribute("href", routes.metrics({ agent_id: "a-1" }));
    expect(await screen.findByRole("link", { name: "anthropic:legacy: open matching runs" })).toBeInTheDocument();
    const paths = metricCalls().map((u) => u.pathname + (u.searchParams.get("by") ? `?by=${u.searchParams.get("by")}` : ""));
    expect(paths.sort()).toEqual([
      "/api/v1/metrics/breakdown?by=agent",
      "/api/v1/metrics/breakdown?by=model",
      "/api/v1/metrics/summary",
      "/api/v1/metrics/timeseries",
    ]);
    const summary = metricCalls().find((u) => u.pathname.endsWith("/summary"))!;
    expect(summary.searchParams.get("compare")).toBe("previous");
    // Default range: 7 days.
    const span = Date.parse(summary.searchParams.get("to")!) - Date.parse(summary.searchParams.get("from")!);
    expect(span).toBe(7 * 24 * 3600 * 1000);
  });

  it("an agent filter in the URL reaches every metrics request (every chart changes)", async () => {
    search = "agent_id=a-1&kind=agent&interval=day";
    mockFetch(okResponder);
    render(<MetricsPage />);
    await screen.findByTestId("kpi-runs-value");
    await waitFor(() => expect(metricCalls()).toHaveLength(4));
    expect(calls.some((u) => u.pathname === "/api/v1/models")).toBe(true);
    for (const u of metricCalls()) {
      expect(u.searchParams.get("agent_id")).toBe("a-1");
      expect(u.searchParams.get("kind")).toBe("agent");
    }
    expect(metricCalls().find((u) => u.pathname.endsWith("/timeseries"))!.searchParams.get("interval")).toBe("day");
  });

  it("choosing an agent writes the filter to the URL", async () => {
    mockFetch(okResponder);
    render(<MetricsPage />);
    await screen.findByRole("option", { name: "Support bot" });
    fireEvent.change(screen.getByLabelText("Agent"), { target: { value: "a-1" } });
    expect(replace).toHaveBeenCalledWith(routes.metrics({ agent_id: "a-1" }), { scroll: false });
  });

  it("Enter on the focused runs chart opens the selected bucket's failed runs", async () => {
    search = "agent_id=a-1";
    mockFetch(okResponder);
    render(<MetricsPage />);
    await screen.findByRole("link", { name: "Nov 12, Failed: 2. Open matching runs" });
    const plot = within(screen.getByTestId("chart-Runs and failures")).getByTestId("chart-plot");
    fireEvent.focus(plot);
    fireEvent.keyDown(plot, { key: "ArrowDown" });
    fireEvent.keyDown(plot, { key: "Enter" });
    const target = new URL(push.mock.calls[0][0], "http://x");
    expect(Object.fromEntries(target.searchParams)).toEqual({
      status: "failed", from: "2026-11-12T00:00:00.000Z", to: "2026-11-13T00:00:00.000Z", agent_id: "a-1",
    });
  });

  it("clicking an error bucket opens that bucket's failed runs", async () => {
    search = "agent_id=a-1";
    mockFetch(okResponder);
    render(<MetricsPage />);
    const bar = await screen.findByRole("link", { name: "Nov 12, Failed: 2. Open matching runs" });
    fireEvent.click(bar);
    expect(push).toHaveBeenCalledTimes(1);
    const target = new URL(push.mock.calls[0][0], "http://x");
    expect(target.pathname).toBe(routes.traces());
    expect(Object.fromEntries(target.searchParams)).toEqual({
      status: "failed", from: "2026-11-12T00:00:00.000Z", to: "2026-11-13T00:00:00.000Z", agent_id: "a-1",
    });
  });

  it("a timeout (503) is shown as a hint to narrow the query", async () => {
    mockFetch((url) =>
      url.pathname.startsWith("/api/v1/metrics/")
        ? { status: 503, body: { detail: "Metrics query timed out; narrow the time range or the filters" } }
        : okResponder(url),
    );
    render(<MetricsPage />);
    const alerts = await screen.findAllByRole("alert");
    expect(alerts.some((a) => /timed out/.test(a.textContent ?? ""))).toBe(true);
  });

  it("an out-of-bounds custom range is rejected without calling the API", async () => {
    search = "range=custom&from=2026-01-01T00:00:00Z&to=2026-11-01T00:00:00Z";
    mockFetch(okResponder);
    render(<MetricsPage />);
    expect(await screen.findByText("the range must be at most 90 days")).toBeInTheDocument();
    expect(metricCalls()).toHaveLength(0);
  });

  it("empty period: explicit message and empty charts", async () => {
    mockFetch((url) => {
      if (url.pathname.endsWith("/summary")) return { status: 200, body: summaryFixture({}, {}) };
      if (url.pathname.endsWith("/timeseries")) return { status: 200, body: timeseriesFixture() };
      return { status: 200, body: breakdownFixture(url.searchParams.get("by") as "agent", []) };
    });
    render(<MetricsPage />);
    expect(await screen.findByTestId("metrics-empty")).toBeInTheDocument();
    await waitFor(() => expect(screen.getAllByText("No data in this period").length).toBeGreaterThanOrEqual(4));
  });
});
