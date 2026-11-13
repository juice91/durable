import React from "react";
import "@testing-library/jest-dom";
import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import MetricsPage from "./page";
import { breakdownFixture, metricValues, summaryFixture, timeseriesFixture } from "@/components/metrics/metricsFixtures";

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

const MODELS = { items: [{ id: "test:echo", provider: "test", name: "echo", wildcard: false, source: "scripted", description: null }] };

const okResponder: Responder = (url) => {
  if (url.pathname === "/api/v1/models") return { status: 200, body: MODELS };
  if (url.pathname.endsWith("/summary")) {
    return {
      status: 200,
      body: summaryFixture(
        { runs: 6, completed: 3, failed: 3, error_rate: 0.5, requests: 2, cost_usd: "0.0100000000" },
        { runs: 3, requests: 1, unpriced_requests: 1 },
      ),
    };
  }
  if (url.pathname.endsWith("/timeseries")) {
    return { status: 200, body: timeseriesFixture([{ runs: 4, failed: 1 }, { runs: 0 }, { runs: 2, failed: 2 }]) };
  }
  if (url.searchParams.get("by") === "agent") return { status: 200, body: breakdownFixture("agent", [{ key: "a-1", label: "Support bot", runs: 6, failed: 3 }]) };
  return { status: 200, body: breakdownFixture("model", [{ key: "test:echo", runs: 6, requests: 2 }]) };
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
const q = (href: string) => Object.fromEntries(new URL(href, "http://x").searchParams);

describe("MetricsPage acceptance", () => {
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

  describe("URL state round-trip", () => {
    it("every filter in the URL reaches every metrics call", async () => {
      search = "range=30d&interval=day&agent_id=a-1&model=test%3Aecho&provider=test&kind=agent";
      mockFetch(okResponder);
      render(<MetricsPage />);
      await waitFor(() => expect(metricCalls().length).toBeGreaterThanOrEqual(4));
      for (const u of metricCalls()) {
        expect(u.searchParams.get("agent_id")).toBe("a-1");
        expect(u.searchParams.get("model")).toBe("test:echo");
        expect(u.searchParams.get("provider")).toBe("test");
        expect(u.searchParams.get("kind")).toBe("agent");
      }
      const ts = metricCalls().find((u) => u.pathname.endsWith("/timeseries"))!;
      expect(ts.searchParams.get("interval")).toBe("day");
      expect(screen.getByLabelText("Date range")).toHaveValue("30d");
      expect(screen.getByLabelText("Interval")).toHaveValue("day");
      expect(screen.getByLabelText("Kind")).toHaveValue("agent");
    });

    it("changing the kind, interval and range writes them to the URL", async () => {
      mockFetch(okResponder);
      render(<MetricsPage />);
      await screen.findByTestId("kpi-runs-value");
      fireEvent.change(screen.getByLabelText("Kind"), { target: { value: "workflow" } });
      expect(replace).toHaveBeenLastCalledWith("/metrics?kind=workflow", { scroll: false });
      fireEvent.change(screen.getByLabelText("Interval"), { target: { value: "day" } });
      expect(replace).toHaveBeenLastCalledWith("/metrics?interval=day", { scroll: false });
      fireEvent.change(screen.getByLabelText("Date range"), { target: { value: "24h" } });
      expect(replace).toHaveBeenLastCalledWith("/metrics?range=24h", { scroll: false });
    });

    it("a custom range in the URL is queried as given", async () => {
      search = "range=custom&from=2026-11-01T00:00:00Z&to=2026-11-03T00:00:00Z";
      mockFetch(okResponder);
      render(<MetricsPage />);
      await waitFor(() => expect(metricCalls().length).toBeGreaterThan(0));
      const s = metricCalls().find((u) => u.pathname.endsWith("/summary"))!;
      expect(s.searchParams.get("from")).toBe("2026-11-01T00:00:00Z");
      expect(s.searchParams.get("to")).toBe("2026-11-03T00:00:00Z");
      expect(s.searchParams.get("compare")).toBe("previous");
    });
  });

  describe("drill-down links to /traces", () => {
    it("KPI cards link to /traces with the page filters", async () => {
      search = "agent_id=a-1&provider=test";
      mockFetch(okResponder);
      render(<MetricsPage />);
      await screen.findByTestId("kpi-runs-value");
      const runs = q(screen.getByTestId("kpi-runs").getAttribute("href")!);
      expect(runs).toMatchObject({ agent_id: "a-1", provider: "test" });
      expect(runs.from).toBeTruthy();
      expect(runs.to).toBeTruthy();
      expect(q(screen.getByTestId("kpi-error_rate").getAttribute("href")!)).toMatchObject({ status: "failed", agent_id: "a-1" });
      expect(new URL(screen.getByTestId("kpi-runs").getAttribute("href")!, "http://x").pathname).toBe("/traces");
    });

    it("breakdown rows link to the agent or model, and failed counts to failed runs", async () => {
      mockFetch(okResponder);
      render(<MetricsPage />);
      const agent = await screen.findByRole("link", { name: "Support bot: open matching runs" });
      expect(q(agent.getAttribute("href")!)).toMatchObject({ agent_id: "a-1" });
      expect(q(screen.getByRole("link", { name: "Support bot: open 3 failed runs" }).getAttribute("href")!)).toMatchObject({ status: "failed", agent_id: "a-1" });
      const model = await screen.findByRole("link", { name: "test:echo: open matching runs" });
      expect(q(model.getAttribute("href")!)).toMatchObject({ model: "test:echo" });
      fireEvent.click(screen.getByTestId("breakdown-row-model-test:echo").querySelector("td")!);
      expect(q(push.mock.calls.at(-1)![0])).toMatchObject({ model: "test:echo" });
    });

    it("the chart is one tab stop; Home, End and Enter open a bucket's runs", async () => {
      mockFetch(okResponder);
      render(<MetricsPage />);
      const plot = (await screen.findAllByTestId("chart-plot"))[0];
      const chart = plot.closest("figure") ?? plot.parentElement!;
      // One tab stop: the plot is the only tabbable element inside the chart outside its closed table.
      const tabbable = Array.from(chart.querySelectorAll<HTMLElement>("a[href], button, [tabindex]")).filter(
        (el) => el.getAttribute("tabindex") !== "-1" && !el.closest("details"),
      );
      expect(tabbable).toEqual([plot]);
      expect(plot).toHaveAttribute("tabindex", "0");
      // Real keyboard flow: DOM focus on the tab stop, then keys (a key press enters keyboard mode).
      act(() => plot.focus());
      expect(document.activeElement).toBe(plot);
      fireEvent.keyDown(plot, { key: "Home" });
      expect(screen.getAllByTestId("active-bucket")[0]).toHaveAttribute("data-bucket", "0");
      fireEvent.keyDown(plot, { key: "End" });
      expect(screen.getAllByTestId("active-bucket")[0]).toHaveAttribute("data-bucket", "2");
      fireEvent.keyDown(plot, { key: "Enter" });
      expect(push).toHaveBeenCalledTimes(1);
      expect(new URL(push.mock.calls[0][0], "http://x").pathname).toBe("/traces");
    });
  });

  describe("range limits and server errors", () => {
    it("refuses a range over 90 days without calling the metrics API", async () => {
      search = "range=custom&from=2026-01-01T00:00:00Z&to=2026-11-01T00:00:00Z";
      mockFetch(okResponder);
      render(<MetricsPage />);
      expect(await screen.findByRole("alert")).toHaveTextContent("the range must be at most 90 days");
      expect(metricCalls()).toHaveLength(0);
    });

    it("refuses hourly buckets over 400 and from after to", async () => {
      search = "range=custom&from=2026-10-01T00:00:00Z&to=2026-11-01T00:00:00Z&interval=hour";
      mockFetch(okResponder);
      const { unmount } = render(<MetricsPage />);
      expect(await screen.findByRole("alert")).toHaveTextContent("at most 400 hour buckets");
      expect(metricCalls()).toHaveLength(0);
      unmount();
      search = "range=custom&from=2026-11-02T00:00:00Z&to=2026-11-01T00:00:00Z";
      render(<MetricsPage />);
      expect(await screen.findByRole("alert")).toHaveTextContent("from must be before to");
      expect(metricCalls()).toHaveLength(0);
    });

    it("shows a 503 as a timeout hint", async () => {
      mockFetch((url) => (url.pathname.startsWith("/api/v1/metrics/") ? { status: 503, body: { detail: "x" } } : okResponder(url)));
      render(<MetricsPage />);
      await waitFor(() =>
        expect(screen.getAllByRole("alert").some((a) => /timed out\. Narrow the time range or add a filter/.test(a.textContent ?? ""))).toBe(true),
      );
    });

    it("shows a 422 message from the server verbatim", async () => {
      mockFetch((url) =>
        url.pathname.startsWith("/api/v1/metrics/") ? { status: 422, body: { detail: "unknown kind 'x'" } } : okResponder(url),
      );
      render(<MetricsPage />);
      await waitFor(() => expect(screen.getAllByRole("alert").some((a) => /unknown kind 'x'/.test(a.textContent ?? ""))).toBe(true));
    });
  });

  describe("KPI and empty states", () => {
    it("the cost delta says there is no previous priced data", async () => {
      mockFetch(okResponder);
      render(<MetricsPage />);
      expect(await screen.findByTestId("kpi-cost-delta")).toHaveTextContent("no previous priced data");
    });

    it("unpriced requests show 'no price' instead of $0", async () => {
      mockFetch((url) =>
        url.pathname.endsWith("/summary")
          ? { status: 200, body: summaryFixture({ runs: 2, requests: 2, unpriced_requests: 2 }, null) }
          : okResponder(url),
      );
      render(<MetricsPage />);
      expect(await screen.findByTestId("kpi-cost-value")).toHaveTextContent("no price");
    });

    it("an empty period shows the explicit message", async () => {
      mockFetch((url) => {
        if (url.pathname.endsWith("/summary")) return { status: 200, body: summaryFixture({}, {}) };
        if (url.pathname.endsWith("/timeseries")) return { status: 200, body: timeseriesFixture([]) };
        if (url.pathname === "/api/v1/models") return { status: 200, body: MODELS };
        return { status: 200, body: breakdownFixture("agent", []) };
      });
      render(<MetricsPage />);
      expect(await screen.findByTestId("metrics-empty")).toHaveTextContent("No runs or model requests match these filters");
      expect(within(document.body).getAllByText("No data in this period").length).toBeGreaterThan(0);
    });

    it("keeps metricValues importable for fixtures", () => {
      expect(metricValues({ runs: 1 }).runs).toBe(1);
    });
  });
});
