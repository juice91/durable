// The former lib/dashboard.ts helpers (P0.18), merged into lib/metrics.ts by SL.7.
import { describe, expect, it } from "vitest";
import {
  ATTENTION_STATUSES,
  attentionCount,
  dashboardParams,
  dashboardPath,
  metricsAgentHref,
  metricsPageHref,
  metricsQueryParams,
  pageStateToFilters,
  parseDashboardState,
  parsePageState,
  recentFailuresFilters,
  recentFailuresHref,
} from "./metrics";
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

  it("round-trips through its own URL, which is now the Metrics page", () => {
    for (const qs of ["", "range=24h", "range=90d", "range=custom&from=2026-11-01T00%3A00%3A00Z&to=2026-11-03T00%3A00%3A00Z"]) {
      const state = parseDashboardState(new URLSearchParams(qs));
      expect(parseDashboardState(dashboardParams(state))).toEqual(state);
    }
    expect(dashboardPath(parseDashboardState(new URLSearchParams("")))).toBe(routes.metrics());
    expect(dashboardPath(parseDashboardState(new URLSearchParams("range=30d")))).toBe(routes.metrics({ range: "30d" }));
    // One literal URL per helper, so a builder bug is caught.
    expect(dashboardPath(parseDashboardState(new URLSearchParams("range=24h")))).toBe("/metrics?range=24h");
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

describe("metricsAgentHref", () => {
  it("narrows Metrics to the agent and keeps the range and every other filter", () => {
    const state = parsePageState(new URLSearchParams("range=30d&model=m-1&kind=agent&interval=day"));
    const params = query(metricsAgentHref(state, "a:1"));
    expect(params.get("agent_id")).toBe("a:1");
    expect(parsePageState(params)).toEqual({ ...state, agent_id: "a:1" });
    expect(metricsAgentHref(parsePageState(new URLSearchParams("")), "a-1")).toBe(routes.metrics({ agent_id: "a-1" }));
  });

  it("replaces an agent filter already set", () => {
    const state = parsePageState(new URLSearchParams("agent_id=a-1"));
    expect(query(metricsAgentHref(state, "a-2")).getAll("agent_id")).toEqual(["a-2"]);
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
      tags: [],
      metadata: [],
      q: null,
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

describe("breakdownPath compare", () => {
  it("asks for the previous period only when told to", async () => {
    const { breakdownPath } = await import("./metrics");
    const F = { from: "2026-11-10T00:00:00+00:00", to: "2026-11-13T00:00:00+00:00" };
    expect(new URL(breakdownPath(F, "agent"), "http://x").searchParams.get("compare")).toBeNull();
    expect(new URL(breakdownPath(F, "agent", "runs", 10, "previous"), "http://x").searchParams.get("compare")).toBe("previous");
  });
});
