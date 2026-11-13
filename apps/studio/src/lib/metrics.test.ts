import { describe, expect, it } from "vitest";
import { ApiError } from "./api";
import {
  breakdownPath,
  bucketCount,
  bucketRange,
  computeDelta,
  defaultInterval,
  formatBucket,
  formatCostWithPricing,
  formatCount,
  formatLatency,
  formatPercent,
  metricsErrorMessage,
  modelFilterOptions,
  niceMax,
  pageStateToFilters,
  pageStateToParams,
  parsePageState,
  presetRange,
  providersOf,
  summarizeSeries,
  summaryPath,
  timeseriesPath,
  tracesHref,
  validateRange,
  DEFAULT_PAGE_STATE,
} from "./metrics";

const F = { from: "2026-11-06T00:00:00.000Z", to: "2026-11-13T00:00:00.000Z" };

describe("query strings", () => {
  it("summary carries every set filter and compare=previous; empty filters are left out", () => {
    const path = summaryPath({ ...F, agent_id: "a1", model: "openai:gpt-4o", provider: null, kind: "agent" });
    const url = new URL(path, "http://x");
    expect(url.pathname).toBe("/api/v1/metrics/summary");
    expect(url.searchParams.get("from")).toBe(F.from);
    expect(url.searchParams.get("to")).toBe(F.to);
    expect(url.searchParams.get("agent_id")).toBe("a1");
    expect(url.searchParams.get("model")).toBe("openai:gpt-4o");
    expect(url.searchParams.get("kind")).toBe("agent");
    expect(url.searchParams.has("provider")).toBe(false);
    expect(url.searchParams.get("compare")).toBe("previous");
  });

  it("timeseries interval and repeated metric params", () => {
    const url = new URL(timeseriesPath(F, "hour", ["runs", "failed"]), "http://x");
    expect(url.searchParams.get("interval")).toBe("hour");
    expect(url.searchParams.getAll("metric")).toEqual(["runs", "failed"]);
    expect(new URL(timeseriesPath(F), "http://x").searchParams.has("interval")).toBe(false);
  });

  it("breakdown by, sort and limit", () => {
    const url = new URL(breakdownPath({ ...F, provider: "anthropic" }, "model", "cost", 5), "http://x");
    expect(Object.fromEntries(url.searchParams)).toMatchObject({ by: "model", sort: "cost", limit: "5", provider: "anthropic" });
  });
});

describe("drill-down links", () => {
  it("links to /traces with the same filters and a comma-joined status", () => {
    const href = tracesHref({ ...F, agent_id: "a1", model: "test:ticket", kind: "agent" }, { status: ["failed"] });
    const url = new URL(href, "http://x");
    expect(url.pathname).toBe("/traces");
    expect(Object.fromEntries(url.searchParams)).toEqual({
      status: "failed", from: F.from, to: F.to, agent_id: "a1", model: "test:ticket", kind: "agent",
    });
  });

  it("overrides narrow the filters (a breakdown row)", () => {
    const url = new URL(tracesHref({ ...F, model: "x:y" }, { model: "openai:gpt-4o" }), "http://x");
    expect(url.searchParams.get("model")).toBe("openai:gpt-4o");
    expect(url.searchParams.has("status")).toBe(false);
  });

  it("bucket ranges are clamped to the queried range", () => {
    const range = { from: "2026-11-12T10:30:00.000Z", to: "2026-11-12T12:15:00.000Z" };
    expect(bucketRange("2026-11-12T10:00:00+00:00", "hour", range)).toEqual({
      from: "2026-11-12T10:30:00.000Z", to: "2026-11-12T11:00:00.000Z",
    });
    expect(bucketRange("2026-11-12T12:00:00+00:00", "hour", range)).toEqual({
      from: "2026-11-12T12:00:00.000Z", to: "2026-11-12T12:15:00.000Z",
    });
    expect(bucketRange("2026-11-07T00:00:00+00:00", "day", F)).toEqual({
      from: "2026-11-07T00:00:00.000Z", to: "2026-11-08T00:00:00.000Z",
    });
  });
});

describe("ranges", () => {
  it("presets end at now (rounded up to the minute)", () => {
    const now = new Date("2026-11-13T10:00:30Z");
    expect(presetRange("24h", now)).toEqual({ from: "2026-11-12T10:01:00.000Z", to: "2026-11-13T10:01:00.000Z" });
    expect(presetRange("7d", now).from).toBe("2026-11-06T10:01:00.000Z");
  });

  it("validates the API bounds", () => {
    expect(validateRange(F.to, F.from)).toBe("from must be before to");
    expect(validateRange("2026-01-01T00:00:00Z", "2026-11-01T00:00:00Z")).toBe("the range must be at most 90 days");
    expect(validateRange(F.from, F.to, "hour")).toBeNull(); // 168 buckets
    expect(validateRange("2026-10-01T00:00:00Z", "2026-11-01T00:00:00Z", "hour")).toMatch(/at most 400 hour buckets/);
    expect(validateRange("nope", F.to)).toMatch(/valid/);
  });

  it("bucket counts and the default interval match the API", () => {
    expect(bucketCount(F.from, F.to, "day")).toBe(7);
    expect(bucketCount("2026-11-12T10:30:00Z", "2026-11-12T12:15:00Z", "hour")).toBe(3);
    expect(defaultInterval("2026-11-11T00:00:00Z", "2026-11-13T00:00:00Z")).toBe("hour");
    expect(defaultInterval(F.from, F.to)).toBe("day");
  });
});

describe("page URL state", () => {
  it("round-trips filters and a custom range", () => {
    const state = { range: "custom" as const, from: F.from, to: F.to, interval: "hour" as const, agent_id: "a1", model: "m:x", provider: "m", kind: "workflow" as const };
    expect(parsePageState(pageStateToParams(state))).toEqual(state);
  });

  it("defaults to 7 days and drops junk values", () => {
    expect(parsePageState(new URLSearchParams("range=bogus&kind=nope&interval=week"))).toEqual(DEFAULT_PAGE_STATE);
    expect(pageStateToParams(DEFAULT_PAGE_STATE).toString()).toBe("");
  });

  it("a link with from/to and no range is a custom range; an invalid custom range falls back", () => {
    expect(parsePageState(new URLSearchParams(`from=${F.from}&to=${F.to}`)).range).toBe("custom");
    expect(parsePageState(new URLSearchParams("range=custom&from=bad")).range).toBe("7d");
  });

  it("resolves to API filters", () => {
    const now = new Date("2026-11-13T00:00:00Z");
    expect(pageStateToFilters({ ...DEFAULT_PAGE_STATE, range: "24h", agent_id: "a1" }, now)).toEqual({
      from: "2026-11-12T00:00:00.000Z", to: "2026-11-13T00:00:00.000Z", agent_id: "a1", model: null, provider: null, kind: null,
    });
  });
});

describe("period deltas", () => {
  it("relative change, direction and tone by polarity", () => {
    const d = computeDelta(150, 100, "lower-better");
    expect(d).toMatchObject({ direction: "up", percent: 50, absolute: 50, tone: "worse" });
    expect(d!.text).toBe("up 50% from 100");
    expect(computeDelta(90, 100, "lower-better")!.tone).toBe("better");
    expect(computeDelta(90, 100, "neutral")!.tone).toBe("neutral");
    expect(computeDelta(105, 100)!.text).toBe("up 5.0% from 100");
  });

  it("previous zero, previous null, flat, current null", () => {
    expect(computeDelta(5, 0)).toMatchObject({ direction: "up", percent: null, text: "up from 0" });
    expect(computeDelta(5, null)!.text).toBe("no data in the previous period");
    expect(computeDelta(5, 5)!.direction).toBe("flat");
    expect(computeDelta(null, 5)).toBeNull();
    expect(computeDelta(5, undefined)).toBeNull();
  });
});

describe("formatting", () => {
  it("unpriced cost is explicit", () => {
    expect(formatCostWithPricing({ cost_usd: "0", requests: 3, unpriced_requests: 3 })).toBe("no price");
    expect(formatCostWithPricing({ cost_usd: "0.5", requests: 3, unpriced_requests: 1 })).toBe("$0.50");
    expect(formatCostWithPricing({ cost_usd: "0", requests: 0, unpriced_requests: 0 })).toBe("$0");
  });

  it("counts, latency, percent, buckets", () => {
    expect(formatCount(1234)).toBe("1,234");
    expect(formatCount(12_345)).toBe("12.3K");
    expect(formatCount(4_500_000)).toBe("4.5M");
    expect(formatLatency(850.4)).toBe("850 ms");
    expect(formatLatency(1250)).toBe("1.3 s");
    expect(formatLatency(null)).toBe("—");
    expect(formatPercent(0.125)).toBe("12.5%");
    expect(formatPercent(0.0123)).toBe("1.2%");
    expect(formatPercent(0)).toBe("0%");
    expect(formatPercent(null)).toBe("—");
    expect(formatBucket("2026-11-10T00:00:00+00:00", "day")).toBe("Nov 10");
    expect(formatBucket("2026-11-10T14:00:00+00:00", "hour")).toBe("Nov 10, 14:00");
  });

  it("error messages: 503 is a timeout hint, 422 verbatim", () => {
    expect(metricsErrorMessage(new ApiError(503, "x"))).toMatch(/timed out/);
    expect(metricsErrorMessage(new ApiError(422, "the range must be at most 90 days"))).toBe("the range must be at most 90 days");
  });
});

describe("chart helpers", () => {
  const buckets = [{ start: "2026-11-10T00:00:00Z" }, { start: "2026-11-11T00:00:00Z" }, { start: "2026-11-12T00:00:00Z" }];

  it("summaries name total, peak and low", () => {
    expect(summarizeSeries("Runs", buckets, [2, 5, 1], "day", String)).toBe(
      "Runs: 8 in total over 3 daily buckets; highest 5 at Nov 11, lowest 1 at Nov 12.",
    );
    expect(summarizeSeries("Runs", buckets, [0, 0, 0], "day", String)).toBe("Runs: no data in 3 daily buckets.");
    expect(summarizeSeries("p50", buckets, [null, 30, null], "day", String, "none")).toBe(
      "p50 over 3 daily buckets; highest 30 at Nov 11, lowest 30 at Nov 11.",
    );
  });

  it("nice axis maxima and providers", () => {
    expect(niceMax(0)).toBe(1);
    expect(niceMax(7)).toBe(10);
    expect(niceMax(180)).toBe(200);
    expect(niceMax(0.0042)).toBe(0.005);
    expect(providersOf(["openai:gpt-4o", "anthropic:claude", "openai:o1"])).toEqual(["anthropic", "openai"]);
  });
});

describe("model filter options", () => {
  it("merges GET /models (minus wildcards) with models seen in metrics; providers include wildcard ones", () => {
    const listed = [
      { id: "openai:gpt-4o", provider: "openai", name: "gpt-4o", wildcard: false, source: "allowlist" as const, description: null },
      { id: "litellm:*", provider: "litellm", name: "*", wildcard: true, source: "allowlist" as const, description: null },
      { id: "test:echo", provider: "test", name: "echo", wildcard: false, source: "scripted" as const, description: "x" },
    ];
    expect(modelFilterOptions(listed, ["litellm:qwen3", "openai:gpt-4o", "anthropic:old"])).toEqual({
      models: ["anthropic:old", "litellm:qwen3", "openai:gpt-4o", "test:echo"],
      providers: ["anthropic", "litellm", "openai", "test"],
    });
    expect(modelFilterOptions([], [])).toEqual({ models: [], providers: [] });
  });

  it("404 explains the API needs the metrics routes", () => {
    expect(metricsErrorMessage(new ApiError(404, "Not Found"))).toMatch(/not available on this server/);
  });
});
