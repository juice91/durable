import { describe, expect, it } from "vitest";
import {
  ATTENTION_STATUSES,
  attentionCount,
  dashboardParams,
  dashboardPath,
  metricsPageHref,
  parseDashboardState,
  recentFailuresFilters,
  recentFailuresHref,
} from "./dashboard";
import { metricsQueryParams, pageStateToFilters, parsePageState } from "./metrics";
import { parseTraceFilters } from "./traces";
import { routes } from "@/lib/routes";

const NOW = new Date("2026-11-13T12:00:00Z");

function query(href: string): URLSearchParams {
  return new URL(href, "http://studio.test").searchParams;
}

describe("parseDashboardState", () => {
  it("defaults to the last 7 days", () => {
    expect(parseDashboardState(new URLSearchParams(""))).toMatchObject({ range: "7d", from: null, to: null });
    expect(parseDashboardState(null).range).toBe("7d");
  });

  it("keeps presets and custom ranges", () => {
    expect(parseDashboardState(new URLSearchParams("range=30d")).range).toBe("30d");
    const custom = parseDashboardState(new URLSearchParams("from=2026-11-01T00:00:00Z&to=2026-11-02T00:00:00Z"));
    expect(custom).toMatchObject({ range: "custom", from: "2026-11-01T00:00:00Z", to: "2026-11-02T00:00:00Z" });
  });

  it("drops dimension filters and the interval (the dashboard shows the whole tenant)", () => {
    const state = parseDashboardState(new URLSearchParams("range=24h&agent_id=a-1&model=m&provider=p&kind=tool&interval=hour"));
    expect(state).toEqual({ range: "24h", from: null, to: null, interval: null, agent_id: null, model: null, provider: null, kind: null });
  });

  it("round-trips through its own URL", () => {
    for (const qs of ["", "range=24h", "range=90d", "range=custom&from=2026-11-01T00%3A00%3A00Z&to=2026-11-03T00%3A00%3A00Z"]) {
      const state = parseDashboardState(new URLSearchParams(qs));
      expect(parseDashboardState(dashboardParams(state))).toEqual(state);
    }
    expect(dashboardPath(parseDashboardState(new URLSearchParams("")))).toBe(routes.dashboard());
    expect(dashboardPath(parseDashboardState(new URLSearchParams("range=30d")))).toBe(routes.dashboard({ range: "30d" }));
    // One literal URL per helper, so a builder bug is caught.
    expect(dashboardPath(parseDashboardState(new URLSearchParams("range=24h")))).toBe("/?range=24h");
  });
});

describe("metricsPageHref", () => {
  it("opens Metrics on the same range, so both pages resolve identical metrics queries", () => {
    for (const qs of ["", "range=24h", "range=30d", "range=90d", "from=2026-11-01T00:00:00Z&to=2026-11-05T00:00:00Z"]) {
      const dashboard = parseDashboardState(new URLSearchParams(qs));
      const href = metricsPageHref(dashboard);
      expect(href.startsWith(routes.metrics())).toBe(true);
      const metrics = parsePageState(query(href));
      const a = metricsQueryParams(pageStateToFilters(dashboard, NOW)).toString();
      const b = metricsQueryParams(pageStateToFilters(metrics, NOW)).toString();
      expect(b).toBe(a);
    }
  });

  it("keeps a preset a preset (no frozen from/to)", () => {
    expect(metricsPageHref(parseDashboardState(new URLSearchParams("range=30d")))).toBe(routes.metrics({ range: "30d" }));
    expect(metricsPageHref(parseDashboardState(new URLSearchParams("")))).toBe(routes.metrics());
    expect(metricsPageHref(parseDashboardState(new URLSearchParams("range=24h")))).toBe("/metrics?range=24h");
  });

  it("narrows to an agent for the top agents drill-down", () => {
    const href = metricsPageHref(parseDashboardState(new URLSearchParams("range=24h")), { agent_id: "a-1" });
    const params = query(href);
    expect(params.get("range")).toBe("24h");
    expect(params.get("agent_id")).toBe("a-1");
    expect(parsePageState(params).agent_id).toBe("a-1");
  });
});

describe("recent failures", () => {
  const filters = { from: "2026-11-10T00:00:00.000Z", to: "2026-11-13T00:00:00.000Z" };

  it("lists failed and needs-attention runs of the range", () => {
    expect(recentFailuresFilters(filters)).toEqual({
      status: ["failed", "needs_attention"],
      from: filters.from,
      to: filters.to,
      model: null,
      provider: null,
      agent_id: null,
      kind: null,
    });
    expect(ATTENTION_STATUSES).toEqual(["failed", "needs_attention"]);
  });

  it("'View all' opens the Traces list with exactly the panel's filters", () => {
    const href = recentFailuresHref(filters);
    expect(href.startsWith(`${routes.traces()}?`)).toBe(true);
    expect(parseTraceFilters(query(href))).toEqual(recentFailuresFilters(filters));
  });

  it("counts failed plus needs-attention runs", () => {
    expect(attentionCount({ failed: 3, needs_attention: 2 })).toBe(5);
    expect(attentionCount({ failed: 0, needs_attention: 0 })).toBe(0);
  });
});
