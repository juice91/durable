import { describe, expect, it, vi } from "vitest";
import React from "react";
import { act, fireEvent, render, screen, within } from "@testing-library/react";
import { TimeSeriesChart, barPath, chartGeometry, linePaths } from "./TimeSeriesChart";
import { chartSpecs } from "./MetricsCharts";
import { comparisonCaption, costDelta, kpiCards, MetricsKpis } from "./MetricsKpis";
import { BreakdownTable } from "./BreakdownTable";
import { MetricsFilterBar } from "./MetricsFilterBar";
import { DEFAULT_PAGE_STATE } from "@/lib/metrics";
import { RANGE, breakdownFixture, metricValues, summaryFixture, timeseriesFixture } from "./metricsFixtures";

const FILTERS = { from: RANGE.from, to: RANGE.to, agent_id: "agent-1", model: null, provider: null, kind: null };

function query(href: string | null | undefined) {
  return Object.fromEntries(new URL(href ?? "", "http://x").searchParams);
}

describe("chart geometry", () => {
  it("bars are anchored to the baseline with rounded tops, nothing for zero height", () => {
    expect(barPath(10, 20, 8, 30)).toBe("M10,50 V24 Q10,20 14,20 H14 Q18,20 18,24 V50 Z");
    expect(barPath(0, 0, 8, 0)).toBe("");
  });

  it("line paths break at nulls", () => {
    expect(linePaths([1, null, 2, 3], (i) => i, (v) => v)).toEqual(["M0.00,1.00", "M2.00,2.00L3.00,3.00"]);
  });

  it("y scale uses a nice max", () => {
    const g = chartGeometry(3, 2, 7);
    expect(g.yMax).toBe(10);
    expect(g.y(10)).toBeLessThan(g.y(0));
  });
});

describe("chartSpecs", () => {
  const ts = timeseriesFixture([
    { runs: 4, failed: 1, latency_p50_ms: 100, latency_p95_ms: 300, input_tokens: 10, output_tokens: 5, cost_usd: "0.0100000000", requests: 2 },
    { runs: 0 },
    { runs: 2, failed: 2, unpriced_requests: 3, requests: 3 },
  ]);

  it("maps buckets to series values", () => {
    const [runs, latency, tokens, cost] = chartSpecs(ts, FILTERS);
    expect(runs.series.map((s) => [s.key, s.values])).toEqual([["runs", [4, 0, 2]], ["failed", [1, 0, 2]]]);
    expect(latency.series[1].values).toEqual([300, null, null]);
    expect(tokens.series.map((s) => s.values)).toEqual([[10, 0, 0], [5, 0, 0]]);
    expect(cost.series[0].values).toEqual([0.01, 0, 0]);
    expect(cost.extraTooltip!(2)).toEqual(["3 requests have no price", "Counted by request time"]);
    expect(runs.summary).toMatch(/^Runs: 6 in total over 3 daily buckets/);
  });

  it("the failed bar of a bucket opens that bucket's failed runs with the page filters", () => {
    const [runs] = chartSpecs(ts, FILTERS);
    const failed = runs.series.find((s) => s.key === "failed")!;
    expect(query(failed.href!(2))).toEqual({
      status: "failed", from: "2026-11-12T00:00:00.000Z", to: "2026-11-13T00:00:00.000Z", agent_id: "agent-1",
    });
    expect(query(runs.bucketHref(0))).toEqual({ from: "2026-11-10T00:00:00.000Z", to: "2026-11-11T00:00:00.000Z", agent_id: "agent-1" });
  });

  it("returns a subset in the requested order (dashboard reuse)", () => {
    expect(chartSpecs(ts, FILTERS, ["cost", "runs"]).map((s) => s.id)).toEqual(["cost", "runs"]);
  });
});

describe("TimeSeriesChart", () => {
  const ts = timeseriesFixture([{ runs: 4, failed: 1 }, { runs: 0 }, { runs: 2, failed: 2 }]);
  const [spec] = chartSpecs(ts, FILTERS);

  it("is one tab stop: the pointer targets are out of the tab order and hidden from assistive tech", () => {
    const { container } = render(<TimeSeriesChart {...spec} buckets={ts.buckets} interval="day" />);
    const plot = screen.getByTestId("chart-plot");
    expect(plot).toHaveAttribute("tabindex", "0");
    expect(plot).toHaveAccessibleName(/^Runs and failures chart\. Use the left and right arrow keys/);
    const svgLinks = container.querySelectorAll("svg a");
    expect(svgLinks).toHaveLength(6); // 3 buckets x 2 series, mouse only
    svgLinks.forEach((a) => expect(a).toHaveAttribute("tabindex", "-1"));
    expect(container.querySelector("svg")).toHaveAttribute("aria-hidden", "true");
    // Only the plot is tabbable inside the chart (table links are inside the closed <details>).
    const tabbable = Array.from(container.querySelectorAll<HTMLElement>("a[href], [tabindex]")).filter(
      (el) => el.getAttribute("tabindex") !== "-1" && !el.closest("details"),
    );
    expect(tabbable).toEqual([plot]);
  });

  it("arrow keys move the active bucket and series, Enter opens its drill-down, the live region reads it", () => {
    const onNavigate = vi.fn();
    render(<TimeSeriesChart {...spec} buckets={ts.buckets} interval="day" onNavigate={onNavigate} />);
    const plot = screen.getByTestId("chart-plot");
    const live = screen.getByTestId("chart-live");
    expect(live).toHaveAttribute("aria-live", "polite");
    fireEvent.focus(plot);
    fireEvent.keyDown(plot, { key: "End" });
    // The most recent bucket, first series.
    expect(live).toHaveTextContent("Nov 12, Runs: 2. Runs: 2, Failed: 2. Press Enter to open matching runs.");
    expect(screen.getByTestId("active-bucket")).toHaveAttribute("data-bucket", "2");
    fireEvent.keyDown(plot, { key: "ArrowDown" });
    expect(live).toHaveTextContent(/^Nov 12, Failed: 2\./);
    expect(screen.getByTestId("active-series")).toBeInTheDocument();
    fireEvent.keyDown(plot, { key: "Enter" });
    expect(query(onNavigate.mock.calls[0][0])).toEqual({
      status: "failed", from: "2026-11-12T00:00:00.000Z", to: "2026-11-13T00:00:00.000Z", agent_id: "agent-1",
    });
    fireEvent.keyDown(plot, { key: "ArrowLeft" });
    expect(live).toHaveTextContent(/^Nov 11, Failed: 0\./);
    fireEvent.keyDown(plot, { key: "Home" });
    fireEvent.keyDown(plot, { key: "ArrowUp" });
    expect(live).toHaveTextContent(/^Nov 10, Runs: 4\./);
    fireEvent.keyDown(plot, { key: " " });
    expect(query(onNavigate.mock.calls[1][0])).toEqual({
      from: "2026-11-10T00:00:00.000Z", to: "2026-11-11T00:00:00.000Z", agent_id: "agent-1",
    });
    fireEvent.keyDown(plot, { key: "ArrowLeft" }); // clamps at the first bucket
    expect(screen.getByTestId("active-bucket")).toHaveAttribute("data-bucket", "0");
    fireEvent.keyDown(plot, { key: "End" });
    expect(screen.getByTestId("active-bucket")).toHaveAttribute("data-bucket", "2");
    fireEvent.blur(plot);
    expect(screen.queryByTestId("active-bucket")).toBeNull();
    expect(live).toHaveTextContent("");
  });

  it("the data table carries the drill-down links; mouse clicks on bars still navigate", () => {
    const onNavigate = vi.fn();
    const { container } = render(<TimeSeriesChart {...spec} buckets={ts.buckets} interval="day" onNavigate={onNavigate} />);
    const fig = screen.getByRole("figure", { name: "Runs and failures" });
    expect(fig).toHaveAccessibleDescription(/Runs: 6 in total/);
    expect(within(fig).getByRole("list", { name: "Runs and failures legend" })).toHaveTextContent("RunsFailed");
    const table = screen.getByRole("table");
    expect(within(table).getAllByRole("row")).toHaveLength(4);
    // 3 bucket links + the non-zero series cells (runs 4, 2; failed 1, 2).
    expect(within(table).getAllByRole("link")).toHaveLength(7);
    const failed = within(table).getByRole("link", { name: "Nov 12, Failed: 2. Open matching runs" });
    expect(query(failed.getAttribute("href"))).toMatchObject({ status: "failed", from: "2026-11-12T00:00:00.000Z" });
    fireEvent.click(failed);
    expect(onNavigate).toHaveBeenLastCalledWith(failed.getAttribute("href"));
    const day = within(table).getByRole("link", { name: "Nov 11: Open matching runs" });
    expect(query(day.getAttribute("href"))).toEqual({ from: "2026-11-11T00:00:00.000Z", to: "2026-11-12T00:00:00.000Z", agent_id: "agent-1" });
    // pointer: hover shows the tooltip, click navigates
    const bar = container.querySelector('svg a[data-bucket="2"][data-series="failed"]')!;
    fireEvent.mouseEnter(bar);
    expect(screen.getByTestId("chart-tooltip")).toHaveTextContent("Nov 12 UTC");
    expect(screen.getByTestId("chart-tooltip")).toHaveTextContent("Runs2");
    fireEvent.click(bar);
    expect(onNavigate).toHaveBeenLastCalledWith(failed.getAttribute("href"));
  });

  it("rapid key presses within one render still move from the latest bucket", () => {
    render(<TimeSeriesChart {...spec} buckets={ts.buckets} interval="day" />);
    const plot = screen.getByTestId("chart-plot");
    act(() => {
      fireEvent.focus(plot);
      fireEvent.keyDown(plot, { key: "ArrowLeft" });
      fireEvent.keyDown(plot, { key: "ArrowLeft" });
      fireEvent.keyDown(plot, { key: "ArrowDown" });
    });
    expect(screen.getByTestId("active-bucket")).toHaveAttribute("data-bucket", "0");
    expect(screen.getByTestId("chart-live")).toHaveTextContent(/^Nov 10, Failed: 1\./);
  });

  it("a refresh with fewer buckets clamps the selection: Enter and ArrowLeft stay in range", () => {
    const onNavigate = vi.fn();
    const five = { ...ts, buckets: [...ts.buckets, ...["2026-11-13", "2026-11-14"].map((d) => ({ ...ts.buckets[0], start: `${d}T00:00:00+00:00` }))] };
    const specFor = (data: typeof ts) => chartSpecs(data, FILTERS, ["runs"])[0];
    const { rerender } = render(<TimeSeriesChart {...specFor(five)} buckets={five.buckets} interval="day" onNavigate={onNavigate} />);
    const plot = screen.getByTestId("chart-plot");
    fireEvent.focus(plot);
    fireEvent.keyDown(plot, { key: "End" });
    expect(screen.getByTestId("active-bucket")).toHaveAttribute("data-bucket", "4");
    // e.g. the preset auto-refresh returns 3 buckets while the plot keeps focus
    rerender(<TimeSeriesChart {...spec} buckets={ts.buckets} interval="day" onNavigate={onNavigate} />);
    expect(screen.queryByTestId("active-bucket")).toBeNull();
    expect(screen.getByTestId("chart-live")).toHaveTextContent("");
    expect(() => fireEvent.keyDown(plot, { key: "Enter" })).not.toThrow();
    expect(query(onNavigate.mock.lastCall![0])).toMatchObject({ from: "2026-11-12T00:00:00.000Z", to: "2026-11-13T00:00:00.000Z" });
    fireEvent.keyDown(plot, { key: "ArrowLeft" });
    expect(screen.getByTestId("active-bucket")).toHaveAttribute("data-bucket", "1");
    expect(screen.getByTestId("chart-live")).toHaveTextContent(/^Nov 11, Runs: 0\./);
  });

  it("the selection stays within a shorter series after a refresh", () => {
    // A selection made on 3 buckets; the data shrinks to 1: no out-of-range highlight, keys clamp.
    const { rerender } = render(<TimeSeriesChart {...spec} buckets={ts.buckets} interval="day" />);
    const plot = screen.getByTestId("chart-plot");
    fireEvent.focus(plot);
    fireEvent.keyDown(plot, { key: "End" });
    rerender(<TimeSeriesChart {...spec} buckets={ts.buckets.slice(0, 1)} interval="day" series={spec.series.map((s) => ({ ...s, values: s.values.slice(0, 1) }))} />);
    const highlight = screen.queryByTestId("active-bucket");
    if (highlight) expect(Number(highlight.getAttribute("data-bucket"))).toBeLessThan(1);
    fireEvent.keyDown(plot, { key: "ArrowRight" });
    expect(screen.getByTestId("active-bucket")).toHaveAttribute("data-bucket", "0");
  });

  it("an error while focused resets the keyboard state: no stale live text or tooltip when data returns", () => {
    const { rerender } = render(<TimeSeriesChart {...spec} buckets={ts.buckets} interval="day" />);
    const plot = screen.getByTestId("chart-plot");
    fireEvent.focus(plot);
    fireEvent.keyDown(plot, { key: "ArrowLeft" });
    expect(screen.getByTestId("chart-live")).not.toHaveTextContent(/^$/);
    rerender(<TimeSeriesChart {...spec} buckets={ts.buckets} interval="day" error="boom" />); // plot unmounts, no blur
    rerender(<TimeSeriesChart {...spec} buckets={ts.buckets} interval="day" />);
    expect(screen.getByTestId("chart-live")).toHaveTextContent("");
    expect(screen.queryByTestId("chart-tooltip")).toBeNull();
    expect(screen.queryByTestId("active-bucket")).toBeNull();
  });

  it("a mouse click focuses the plot without keyboard mode: the tooltip clears on leave", () => {
    const onNavigate = vi.fn();
    const { container } = render(<TimeSeriesChart {...spec} buckets={ts.buckets} interval="day" onNavigate={onNavigate} />);
    const plot = screen.getByTestId("chart-plot");
    const bar = container.querySelector('svg a[data-bucket="0"][data-series="runs"]')!;
    fireEvent.mouseEnter(bar);
    fireEvent.focus(plot); // focus from the click (not :focus-visible)
    fireEvent.click(bar, { metaKey: true }); // modifier-click: browser handles it, no client navigation
    expect(onNavigate).not.toHaveBeenCalled();
    expect(screen.getByTestId("chart-live")).toHaveTextContent("");
    expect(screen.queryByTestId("active-series")).toBeNull();
    fireEvent.mouseLeave(plot);
    expect(screen.queryByTestId("chart-tooltip")).toBeNull();
  });

  it("points screen-reader users to a reachable, uniquely named data table toggle", () => {
    render(<TimeSeriesChart {...spec} buckets={ts.buckets} interval="day" />);
    const plot = screen.getByTestId("chart-plot");
    expect(plot.getAttribute("aria-label")).toContain('With a screen reader, use the data table below ("Show data table")');
    const toggle = screen.getByText("Show data table");
    expect(toggle.tagName).toBe("SUMMARY");
    expect(toggle).toHaveAccessibleName("Show data table: Runs and failures");
    // The toggle is the next focusable element after the plot.
    const order = Array.from(document.querySelectorAll<HTMLElement>("[tabindex='0'], summary"));
    expect(order.indexOf(toggle)).toBe(order.indexOf(plot) + 1);
  });

  it("line charts drill down per bucket and gap null values", () => {
    const onNavigate = vi.fn();
    const [, latency] = chartSpecs(timeseriesFixture([{ latency_p50_ms: 100, latency_p95_ms: 200 }]), FILTERS);
    render(<TimeSeriesChart {...latency} buckets={timeseriesFixture().buckets} interval="day" onNavigate={onNavigate} />);
    const links = within(screen.getByRole("table")).getAllByRole("link");
    expect(links.map((l) => l.textContent)).toEqual(["Nov 10", "Nov 11", "Nov 12"]);
    const plot = screen.getByTestId("chart-plot");
    expect(plot).toHaveAccessibleName(/move between days, and Enter to open matching runs\. With a screen reader, use the data table below/);
    fireEvent.focus(plot);
    fireEvent.keyDown(plot, { key: "End" });
    expect(screen.getByTestId("chart-live")).toHaveTextContent("Nov 12: p50: no data, p95: no data, p99: no data. Press Enter to open matching runs.");
    fireEvent.keyDown(plot, { key: "ArrowDown" }); // no series selection on line charts
    fireEvent.keyDown(plot, { key: "Enter" });
    expect(onNavigate).toHaveBeenCalledWith(links[2].getAttribute("href"));
  });

  it("usage charts say the drill-down lists runs created in the window", () => {
    const usage = timeseriesFixture([{ cost_usd: "0.5", requests: 2, input_tokens: 5 }]);
    const [tokens, cost] = chartSpecs(usage, FILTERS, ["tokens", "cost"]);
    render(<TimeSeriesChart {...cost} buckets={usage.buckets} interval="day" />);
    expect(screen.getByRole("link", { name: "Nov 10: Open runs created in this window" })).toBeInTheDocument();
    fireEvent.focus(screen.getByTestId("chart-plot"));
    fireEvent.keyDown(screen.getByTestId("chart-plot"), { key: "Home" });
    expect(screen.getByTestId("chart-tooltip")).toHaveTextContent("Counted by request time");
    expect(screen.getByTestId("chart-tooltip")).toHaveTextContent("Select to open runs created in this window");
    expect(tokens.drillLabel).toBe("Open runs created in this window");
    expect(chartSpecs(usage, FILTERS, ["runs"])[0].drillLabel).toBeUndefined();
  });

  it("empty, loading and error states", () => {
    const { rerender } = render(<TimeSeriesChart {...spec} buckets={timeseriesFixture().buckets} interval="day" series={[{ ...spec.series[0], values: [0, 0, 0] }]} />);
    expect(screen.getByText("No data in this period")).toBeInTheDocument();
    expect(screen.queryByRole("table")).toBeNull();
    expect(screen.queryByTestId("chart-plot")).toBeNull();
    rerender(<TimeSeriesChart {...spec} buckets={[]} interval="day" loading />);
    expect(screen.getByLabelText("Loading Runs and failures")).toBeInTheDocument();
    rerender(<TimeSeriesChart {...spec} buckets={[]} interval="day" error="boom" />);
    expect(screen.getByRole("alert")).toHaveTextContent("boom");
  });

  it("uses palette variables only (no hard-coded colours in marks)", () => {
    const { container } = render(<TimeSeriesChart {...spec} buckets={ts.buckets} interval="day" />);
    const fills = Array.from(container.querySelectorAll("path[fill]")).map((p) => p.getAttribute("fill"));
    expect(fills.length).toBeGreaterThan(0);
    for (const f of fills) expect(f).toMatch(/^var\(--viz-/);
    expect(container.querySelector("figure")).toHaveClass("eap-viz");
  });
});

describe("KPI cards", () => {
  it("values, deltas against the previous period and drill-down links", () => {
    const summary = summaryFixture(
      { runs: 120, completed: 100, failed: 20, error_rate: 20 / 120, latency_p50_ms: 900, latency_p95_ms: 2500, input_tokens: 1000, output_tokens: 500, requests: 10, cost_usd: "1.5000000000" },
      { runs: 100, completed: 95, failed: 5, error_rate: 0.05, latency_p50_ms: 1000, latency_p95_ms: 2500, input_tokens: 1000, output_tokens: 500, requests: 10, cost_usd: "1.0000000000" },
    );
    const cards = Object.fromEntries(kpiCards(summary, FILTERS).map((c) => [c.id, c]));
    expect(cards.runs.value).toBe("120");
    expect(cards.runs.delta!.text).toBe("up 20% from 100");
    expect(cards.error_rate.value).toBe("16.7%");
    expect(cards.error_rate.delta!.tone).toBe("worse");
    expect(query(cards.error_rate.href)).toMatchObject({ status: "failed", agent_id: "agent-1" });
    expect(cards.latency_p50.delta!.tone).toBe("better");
    expect(cards.latency_p95.delta!.direction).toBe("flat");
    expect(cards.tokens.value).toBe("1,500");
    expect(cards.cost.value).toBe("$1.50");
    expect(cards.cost.delta!.text).toBe("up 50% from $1.00");
    expect(cards.runs.deltaLabel).toBe("vs previous period");
    expect(comparisonCaption(summary)).toBe("Changes compare with the previous period, Nov 7, 00:00 – Nov 10, 00:00 UTC.");
    expect(comparisonCaption(summaryFixture({}, null))).toBeNull();
  });

  it("unpriced cost is explicit and has no delta when nothing is priced", () => {
    const cards = Object.fromEntries(kpiCards(summaryFixture({ requests: 4, unpriced_requests: 4 }), FILTERS).map((c) => [c.id, c]));
    expect(cards.cost.value).toBe("no price");
    expect(cards.cost.delta).toBeNull();
    expect(cards.cost.hint).toBe("4 of 4 requests have no price and are not included");
  });

  it("cost delta: no previous priced data instead of 'up from $0.00'", () => {
    const priced = metricValues({ requests: 2, cost_usd: "0.4" });
    expect(costDelta(priced, metricValues({ requests: 3, unpriced_requests: 3 }))).toMatchObject({ text: "no previous priced data", noPrevious: true });
    expect(costDelta(priced, metricValues())).toMatchObject({ text: "no previous priced data" });
    expect(costDelta(priced, metricValues({ requests: 1, cost_usd: "0.2" }))!.text).toBe("up 100% from $0.20");
    expect(costDelta(priced, null)).toBeNull();
    const cards = Object.fromEntries(kpiCards(summaryFixture({ requests: 2, cost_usd: "0.4" }, { requests: 0 }), FILTERS).map((c) => [c.id, c]));
    expect(cards.cost.delta!.text).toBe("no previous priced data");
    render(<MetricsKpis summary={summaryFixture({ requests: 2, cost_usd: "0.4" }, { requests: 0 })} filters={FILTERS} ids={["cost"]} />);
    expect(screen.getByTestId("kpi-cost-delta")).toHaveTextContent(/^– no previous priced data$/);
  });

  it("breakdown items carry blocked: null (typed), summaries a count", () => {
    const item = breakdownFixture("agent", [{ key: "a" }]).items[0];
    expect(item.blocked).toBeNull();
    const cards = Object.fromEntries(kpiCards(summaryFixture({ completed: 1, failed: 2, blocked: 1 }), FILTERS).map((c) => [c.id, c]));
    expect(cards.error_rate.hint).toBe("2 failed of 3 finished · 1 blocked");
  });

  it("renders cards as links and a loading row without a summary", () => {
    const { rerender } = render(<MetricsKpis summary={summaryFixture({ runs: 3, completed: 3 }, null)} filters={FILTERS} />);
    expect(screen.getByTestId("kpi-runs")).toHaveAttribute("href", expect.stringMatching(/^\/traces\?/));
    expect(screen.getByTestId("kpi-runs-value")).toHaveTextContent("3");
    expect(screen.queryByTestId("kpi-runs-delta")).toBeNull();
    rerender(<MetricsKpis summary={summaryFixture({ runs: 3, error_rate: 0.5 }, { runs: 1, error_rate: null })} filters={FILTERS} />);
    expect(screen.getByTestId("kpi-error_rate-delta")).toHaveTextContent(/^– no data in the previous period$/);
    expect(screen.getByTestId("kpi-runs-delta")).toHaveTextContent("▲ up 200% from 1 vs previous period");
    rerender(<MetricsKpis summary={null} filters={FILTERS} loading ids={["runs", "cost"]} />);
    expect(screen.getByLabelText("Loading Runs")).toBeInTheDocument();
    expect(screen.getByLabelText("Loading Cost (USD)")).toBeInTheDocument();
  });
});

describe("BreakdownTable", () => {
  it("rows link to the filtered runs; deleted agents and unpriced cost are labelled", () => {
    const onNavigate = vi.fn();
    const data = breakdownFixture("agent", [
      { key: "a-1", label: "Support bot", runs: 10, failed: 2, error_rate: 0.2, requests: 4, cost_usd: "0.2000000000", unpriced_requests: 1 },
      { key: "deadbeef-0000", label: null, runs: 1, requests: 2, unpriced_requests: 2 },
    ]);
    render(<BreakdownTable title="By agent" by="agent" items={data.items} filters={FILTERS} sort="runs" onSortChange={vi.fn()} onNavigate={onNavigate} />);
    const link = screen.getByRole("link", { name: "Support bot: open matching runs" });
    expect(query(link.getAttribute("href"))).toMatchObject({ agent_id: "a-1", from: RANGE.from });
    expect(query(screen.getByRole("link", { name: "Support bot: open 2 failed runs" }).getAttribute("href"))).toMatchObject({ status: "failed", agent_id: "a-1" });
    expect(screen.getByText("Deleted agent (deadbeef)")).toBeInTheDocument();
    expect(screen.getByText("+1 unpriced")).toBeInTheDocument();
    expect(screen.getByText("no price")).toBeInTheDocument();
    fireEvent.click(screen.getByTestId("breakdown-row-agent-a-1").querySelector("td")!);
    expect(onNavigate).toHaveBeenCalledWith(link.getAttribute("href"));
  });

  it("model rows filter by model; empty and error states", () => {
    const { rerender } = render(
      <BreakdownTable title="By model" by="model" items={breakdownFixture("model", [{ key: "openai:gpt-4o", runs: 1 }]).items} filters={FILTERS} sort="requests" />,
    );
    expect(query(screen.getByRole("link", { name: "openai:gpt-4o: open matching runs" }).getAttribute("href"))).toMatchObject({ model: "openai:gpt-4o" });
    rerender(<BreakdownTable title="By model" by="model" items={[]} filters={FILTERS} sort="requests" />);
    expect(screen.getByText("No data in this period")).toBeInTheDocument();
    rerender(<BreakdownTable title="By model" by="model" items={null} filters={FILTERS} sort="requests" error="nope" />);
    expect(screen.getByRole("alert")).toHaveTextContent("nope");
  });
});

describe("MetricsFilterBar", () => {
  it("changes filters, keeps an unknown selected model as an option, clears dimension filters", () => {
    const onChange = vi.fn();
    const state = { ...DEFAULT_PAGE_STATE, model: "x:legacy", kind: "agent" as const };
    render(
      <MetricsFilterBar state={state} onChange={onChange} range={FILTERS} agents={[{ id: "a-1", name: "Support bot" }]} models={["openai:gpt-4o"]} providers={["openai"]} />,
    );
    fireEvent.change(screen.getByLabelText("Agent"), { target: { value: "a-1" } });
    expect(onChange).toHaveBeenLastCalledWith({ ...state, agent_id: "a-1" });
    expect(within(screen.getByLabelText("Model")).getByRole("option", { name: "x:legacy" })).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Date range"), { target: { value: "30d" } });
    expect(onChange).toHaveBeenLastCalledWith({ ...state, range: "30d", from: null, to: null });
    fireEvent.click(screen.getByRole("button", { name: "Clear filters" }));
    expect(onChange).toHaveBeenLastCalledWith(DEFAULT_PAGE_STATE);
  });

  it("disables hourly for long ranges and validates a custom range", () => {
    const onChange = vi.fn();
    const state = { ...DEFAULT_PAGE_STATE, range: "custom" as const, from: "2026-11-01T00:00:00.000Z", to: "2026-11-30T00:00:00.000Z" };
    render(<MetricsFilterBar state={state} onChange={onChange} range={{ from: state.from, to: state.to }} />);
    expect(within(screen.getByLabelText("Interval")).getByRole("option", { name: /Hourly/ })).toBeDisabled();
    fireEvent.change(screen.getByLabelText("From"), { target: { value: "2026-12-01T00:00" } });
    fireEvent.click(screen.getByRole("button", { name: "Apply" }));
    expect(screen.getByRole("alert")).toHaveTextContent("from must be before to");
    expect(onChange).not.toHaveBeenCalled();
    fireEvent.change(screen.getByLabelText("From"), { target: { value: "2026-11-20T00:00" } });
    fireEvent.click(screen.getByRole("button", { name: "Apply" }));
    expect(onChange).toHaveBeenCalledWith(expect.objectContaining({ range: "custom", from: "2026-11-20T00:00:00.000Z" }));
  });
});
