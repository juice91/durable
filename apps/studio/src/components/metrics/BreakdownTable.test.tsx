import React from "react";
import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { BreakdownTable, runsChange } from "./BreakdownTable";
import { chartSpecs } from "./MetricsCharts";
import { breakdownFixture, metricValues, timeseriesFixture } from "./metricsFixtures";

const FILTERS = { from: "2026-11-10T00:00:00+00:00", to: "2026-11-13T00:00:00+00:00" };

function table(items: ReturnType<typeof breakdownFixture>["items"]) {
  render(<BreakdownTable title="Top agents" by="agent" items={items} filters={FILTERS} sort="runs" />);
}

describe("BreakdownTable Change column", () => {
  it("shows the runs delta as a signed percentage", () => {
    const { items } = breakdownFixture("agent", [
      { key: "a1", label: "Up", runs: 17, previous: metricValues({ runs: 11 }) },
      { key: "a2", label: "Down", runs: 5, previous: metricValues({ runs: 10 }) },
      { key: "a3", label: "Same", runs: 4, previous: metricValues({ runs: 4 }) },
    ]);
    table(items);
    expect(screen.getByRole("columnheader", { name: "Change" })).toBeInTheDocument();
    expect(screen.getByTestId("breakdown-change-agent-a1")).toHaveTextContent("+54.5%");
    expect(screen.getByTestId("breakdown-change-agent-a2")).toHaveTextContent("-50.0%");
    expect(screen.getByTestId("breakdown-change-agent-a3")).toHaveTextContent("0.0%");
  });

  it("is blank when the previous period is missing or had no runs", () => {
    const { items } = breakdownFixture("agent", [
      { key: "a1", label: "New", runs: 3, previous: null },
      { key: "a2", label: "Zero", runs: 3, previous: metricValues({ runs: 0 }) },
    ]);
    table(items);
    expect(screen.getByTestId("breakdown-change-agent-a1")).toBeEmptyDOMElement();
    expect(screen.getByTestId("breakdown-change-agent-a2")).toBeEmptyDOMElement();
    expect(runsChange(items[0])).toBe("");
  });

  it("has no Change column without a comparison", () => {
    const { items } = breakdownFixture("agent", [{ key: "a1", label: "Plain", runs: 3 }]);
    table(items);
    expect(screen.queryByRole("columnheader", { name: "Change" })).toBeNull();
    expect(within(screen.getByRole("table")).getAllByRole("columnheader").length).toBeGreaterThan(5);
  });
});

describe("latency chart", () => {
  it("draws p99 as a third line", () => {
    const [, latency] = chartSpecs(
      timeseriesFixture([{ latency_p50_ms: 100, latency_p95_ms: 200, latency_p99_ms: 900 }]),
      FILTERS,
    );
    expect(latency.series.map((s) => s.label)).toEqual(["p50", "p95", "p99"]);
    expect(latency.series[2].values).toContain(900);
  });
});
