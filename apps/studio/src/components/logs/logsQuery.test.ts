import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "@/lib/api";
import {
  EMPTY_LOG_FILTERS,
  appendOlder,
  classifyLogsError,
  isoToMicros,
  logFiltersProblem,
  logFiltersToParams,
  logsPath,
  logsQueryString,
  logsRequestParams,
  microsToIso,
  olderPageTo,
  parseLogFilters,
  runLogsWindow,
  searchLogs,
  traceLogsParams,
  ZERO_SPAN_PAD_MS,
  type LogItem,
} from "./logsQuery";
import { routes } from "@/lib/routes";

const RUN = "11111111-2222-3333-4444-555555555555";
const AGENT = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee";

function item(ts: string, message = "m", run_id: string | null = RUN): LogItem {
  return { ts, level: "info", message, service: "eap-worker", trace_id: null, span_id: null, run_id };
}

describe("URL filters", () => {
  it("round-trips every filter", () => {
    const params = new URLSearchParams({ q: 'a "b" | c', level: "warn", agent_id: AGENT, run_id: RUN, range: "24h" });
    const filters = parseLogFilters(params);
    expect(filters).toEqual({ q: 'a "b" | c', level: "warn", agent_id: AGENT, run_id: RUN, range: "24h", from: null, to: null });
    expect(parseLogFilters(logFiltersToParams(filters))).toEqual(filters);
  });

  it("drops unknown levels and ranges, and a custom range wins over a preset", () => {
    expect(parseLogFilters(new URLSearchParams("level=trace&range=2y"))).toEqual(EMPTY_LOG_FILTERS);
    const f = parseLogFilters(new URLSearchParams("range=1h&from=2026-11-13T10:00:00Z"));
    expect(f.range).toBeNull();
    expect(f.from).toBe("2026-11-13T10:00:00Z");
    expect(logFiltersToParams({ ...f, range: "1h" }).has("range")).toBe(false);
  });

  it("keeps the search text's inner whitespace and caps it at 500 characters", () => {
    expect(parseLogFilters(new URLSearchParams({ q: "  two  words " })).q).toBe("  two  words ");
    expect(parseLogFilters(new URLSearchParams({ q: "   " })).q).toBeNull();
    expect(parseLogFilters(new URLSearchParams({ q: "x".repeat(600) })).q).toHaveLength(500);
  });

  it("builds Logs page links", () => {
    expect(logsPath({})).toBe(routes.logs());
    expect(logsPath({ run_id: RUN })).toBe(`${routes.logs()}?run_id=${RUN}`);
    // One literal URL per helper, so a builder bug is caught.
    expect(logsPath({ q: "a b", level: "error" })).toBe("/logs?q=a+b&level=error");
  });
});

describe("logFiltersProblem", () => {
  it("accepts empty and valid filters", () => {
    expect(logFiltersProblem(EMPTY_LOG_FILTERS)).toBeNull();
    expect(logFiltersProblem({ ...EMPTY_LOG_FILTERS, run_id: RUN, agent_id: AGENT })).toBeNull();
  });
  it("rejects a non-UUID run, a reversed range and a range over 7 days", () => {
    expect(logFiltersProblem({ ...EMPTY_LOG_FILTERS, run_id: "abc" })).toBe("Run id must be a UUID");
    expect(logFiltersProblem({ ...EMPTY_LOG_FILTERS, from: "2026-11-13T10:00:00Z", to: "2026-11-13T09:00:00Z" })).toMatch(/before/);
    expect(logFiltersProblem({ ...EMPTY_LOG_FILTERS, from: "2026-11-01T00:00:00Z", to: "2026-11-13T00:00:00Z" })).toMatch(/7 days/);
    expect(logFiltersProblem({ ...EMPTY_LOG_FILTERS, from: "nope" })).toMatch(/From/);
  });
});

describe("logsRequestParams", () => {
  const now = Date.parse("2026-11-13T12:00:00Z");
  it("turns a preset into `from` relative to now and leaves `to` to the API", () => {
    const p = logsRequestParams({ ...EMPTY_LOG_FILTERS, range: "15m", q: "boom", level: "error", run_id: RUN }, now);
    expect(p).toEqual({ limit: 100, run_id: RUN, q: "boom", level: "error", from: "2026-11-13T11:45:00.000Z" });
  });
  it("passes a custom range through, with the agent", () => {
    const p = logsRequestParams({ ...EMPTY_LOG_FILTERS, agent_id: AGENT, from: "2026-11-13T10:00:00Z", to: "2026-11-13T11:00:00Z" }, now);
    expect(p).toEqual({ limit: 100, agent_id: AGENT, from: "2026-11-13T10:00:00Z", to: "2026-11-13T11:00:00Z" });
  });
  it("sends no window for the default range", () => {
    expect(logsRequestParams(EMPTY_LOG_FILTERS, now)).toEqual({ limit: 100 });
  });
  it("encodes the query string", () => {
    expect(logsQueryString({ q: 'a&b="c"', run_id: RUN, limit: 5 })).toBe(`run_id=${RUN}&q=a%26b%3D%22c%22&limit=5`);
  });
});

describe("paging cursor", () => {
  it("reads the API's microsecond timestamps, with or without a fraction", () => {
    expect(isoToMicros("2026-11-13T10:00:00.123456+00:00")).toBe(Date.parse("2026-11-13T10:00:00Z") * 1000 + 123456);
    expect(isoToMicros("2026-11-13T10:00:00+00:00")).toBe(Date.parse("2026-11-13T10:00:00Z") * 1000);
    expect(isoToMicros("2026-11-13T10:00:00.5Z")).toBe(Date.parse("2026-11-13T10:00:00Z") * 1000 + 500000);
    expect(isoToMicros("yesterday")).toBeNull();
  });

  it("formats microseconds back, carrying into the next second", () => {
    const base = Date.parse("2026-11-13T10:00:00Z") * 1000;
    expect(microsToIso(base + 999_999 + 1)).toBe("2026-11-13T10:00:01.000000Z");
    expect(microsToIso(base + 42)).toBe("2026-11-13T10:00:00.000042Z");
  });

  it("asks for the next page up to one microsecond after the oldest record", () => {
    const items = [item("2026-11-13T10:00:02.000010+00:00"), item("2026-11-13T10:00:01.999999+00:00"), item("2026-11-13T10:00:03+00:00")];
    expect(olderPageTo(items)).toBe("2026-11-13T10:00:02.000000Z");
    expect(olderPageTo([])).toBeNull();
  });

  it("appends older records without duplicates and counts what was new", () => {
    const a = item("2026-11-13T10:00:02+00:00", "a");
    const b = item("2026-11-13T10:00:01+00:00", "b");
    const c = item("2026-11-13T10:00:00+00:00", "c");
    expect(appendOlder([a, b], [b, c])).toEqual({ items: [a, b, c], added: 1 });
    const same = [a, b];
    expect(appendOlder(same, [b]).items).toBe(same);
    expect(appendOlder(same, [b]).added).toBe(0);
  });
});

describe("agent filter (follow-up 2: an API parameter)", () => {
  it("sends agent_id with the other filters", () => {
    const params = logsRequestParams({ ...EMPTY_LOG_FILTERS, agent_id: AGENT, level: "warn" }, Date.parse("2026-11-13T10:00:00Z"));
    expect(params).toEqual({ limit: 100, agent_id: AGENT, level: "warn" });
    expect(logsQueryString(params)).toBe(`agent_id=${AGENT}&level=warn&limit=100`);
    expect(logsRequestParams(EMPTY_LOG_FILTERS)).not.toHaveProperty("agent_id");
  });
});

describe("traceLogsParams", () => {
  it("narrows to the span window with an inclusive end", () => {
    expect(traceLogsParams(RUN, { start: "2026-11-13T10:00:00.250Z", end: "2026-11-13T10:00:01.500Z" }, true)).toEqual({
      run_id: RUN,
      limit: 500,
      from: "2026-11-13T10:00:00.250000Z",
      to: "2026-11-13T10:00:01.501000Z",
    });
  });
  it("gives a zero-length span a window padded both ways, so its own log lines show (browser check)", () => {
    // Was [start, start + 1 ms): a blocking log line a few ms off the span's instant was missed.
    const p = traceLogsParams(RUN, { start: "2026-11-13T10:00:00Z", end: "2026-11-13T10:00:00Z" }, true);
    expect(ZERO_SPAN_PAD_MS).toBe(250);
    expect(p.from).toBe("2026-11-13T09:59:59.750000Z");
    expect(p.to).toBe("2026-11-13T10:00:00.251000Z");
    // An end before the start (clock skew) is treated as zero length too.
    const skew = traceLogsParams(RUN, { start: "2026-11-13T10:00:00Z", end: "2026-11-13T09:59:59.990Z" }, true);
    expect([skew.from, skew.to]).toEqual([p.from, p.to]);
  });
  it("runs an open span to now, and falls back to the whole run without a start or when not narrowed", () => {
    expect(traceLogsParams(RUN, { start: "2026-11-13T10:00:00Z", end: null }, true)).toEqual({ run_id: RUN, limit: 500, from: "2026-11-13T10:00:00.000000Z" });
    expect(traceLogsParams(RUN, { start: null, end: null }, true)).toEqual({ run_id: RUN, limit: 500 });
    expect(traceLogsParams(RUN, { start: "2026-11-13T10:00:00Z", end: "2026-11-13T10:00:01Z" }, false)).toEqual({ run_id: RUN, limit: 500 });
  });
});

describe("runLogsWindow", () => {
  const now = Date.parse("2026-11-13T12:00:00Z");
  it("spans a finished run's lifetime: a minute before creation to 5 s after its finish", () => {
    expect(runLogsWindow({ created_at: "2026-11-13T10:00:00Z", finished_at: "2026-11-13T10:00:30.5Z", display_status: "completed" }, now)).toEqual({
      from: "2026-11-13T09:59:00.000000Z",
      to: "2026-11-13T10:00:35.500000Z",
    });
  });
  it("uses updated_at for a finished run without finished_at, and reads a naive created_at as UTC", () => {
    expect(runLogsWindow({ created_at: "2026-11-13T10:00:00", updated_at: "2026-11-13T10:00:05Z", display_status: "failed" }, now)).toEqual({
      from: "2026-11-13T09:59:00.000000Z",
      to: "2026-11-13T10:00:10.000000Z",
    });
  });
  it("finds an 8-day-old short run (the API default would end at now and miss it)", () => {
    const w = runLogsWindow({ created_at: "2026-11-05T10:00:00Z", finished_at: "2026-11-05T10:00:02Z", display_status: "completed" }, now)!;
    expect(w).toEqual({ from: "2026-11-05T09:59:00.000000Z", to: "2026-11-05T10:00:07.000000Z" });
  });
  it("leaves a running run's end to the API's clock", () => {
    expect(runLogsWindow({ created_at: "2026-11-13T11:00:00Z", updated_at: "2026-11-13T11:30:00Z", display_status: "running" }, now)).toEqual({
      from: "2026-11-13T10:59:00.000000Z",
    });
  });
  it("leaves the whole window to the API for a running run started over 7 days - 1 hour ago", () => {
    expect(runLogsWindow({ created_at: "2026-11-01T00:00:00Z", display_status: "running" }, now)).toEqual({});
    // Just inside the margin: still an explicit start.
    expect(runLogsWindow({ created_at: "2026-11-06T13:01:00Z", display_status: "running" }, now)).toEqual({ from: "2026-11-06T13:00:00.000000Z" });
    expect(runLogsWindow({ created_at: "2026-11-06T12:59:00Z", display_status: "running" }, now)).toEqual({});
  });
  it("caps a long finished run at 7 days, keeping the end", () => {
    expect(runLogsWindow({ created_at: "2026-11-01T00:00:00Z", finished_at: "2026-11-12T00:00:00Z", display_status: "failed" }, now)).toEqual({
      from: "2026-11-05T00:00:05.000000Z",
      to: "2026-11-12T00:00:05.000000Z",
    });
  });
  it("leaves the end to the API while the 5 s after the finish have not passed", () => {
    expect(runLogsWindow({ created_at: "2026-11-13T11:59:50Z", finished_at: "2026-11-13T11:59:58Z", display_status: "completed" }, now)).toEqual({
      from: "2026-11-13T11:58:50.000000Z",
    });
  });
  it("is null for an unreadable created_at", () => {
    expect(runLogsWindow({ created_at: "soon" }, now)).toBeNull();
  });
  it("is used for the trace panel's whole-run mode and the page's run without a range", () => {
    const run = { created_at: "2026-11-13T10:00:00Z", finished_at: "2026-11-13T10:00:10Z", display_status: "completed" };
    const lifetime = { from: "2026-11-13T09:59:00.000000Z", to: "2026-11-13T10:00:15.000000Z" };
    expect(traceLogsParams(RUN, { start: "2026-11-13T10:00:01Z", end: null }, false, 500, run, now)).toEqual({ run_id: RUN, limit: 500, ...lifetime });
    expect(traceLogsParams(RUN, { start: null, end: null }, true, 500, run, now)).toEqual({ run_id: RUN, limit: 500, ...lifetime });
    expect(logsRequestParams({ ...EMPTY_LOG_FILTERS, run_id: RUN }, now, 100, run)).toEqual({ limit: 100, run_id: RUN, ...lifetime });
    // An explicit range wins over the run's lifetime.
    expect(logsRequestParams({ ...EMPTY_LOG_FILTERS, run_id: RUN, range: "1h" }, now, 100, run)).toEqual({ limit: 100, run_id: RUN, from: "2026-11-13T11:00:00.000Z" });
  });
});

describe("classifyLogsError", () => {
  it("maps API statuses to UI states", () => {
    expect(classifyLogsError(new ApiError(503, "Log search is unavailable"))).toEqual({ kind: "unavailable", message: "Log search is unavailable" });
    expect(classifyLogsError(new ApiError(503, "Log search is not configured")).kind).toBe("unavailable");
    expect(classifyLogsError(new ApiError(404, "Run not found"))).toEqual({ kind: "not_found", message: "Run not found" });
    expect(classifyLogsError(new ApiError(422, "the time range may span at most 7 days"))).toEqual({ kind: "invalid", message: "the time range may span at most 7 days" });
    expect(classifyLogsError(new ApiError(502, "x"))).toEqual({ kind: "failed", message: "Log search failed" });
    expect(classifyLogsError(new TypeError("fetch failed"))).toEqual({ kind: "failed", message: "Could not reach the API" });
  });
});

describe("searchLogs", () => {
  const original = process.env.NEXT_PUBLIC_EAP_DEV_TOKEN;
  beforeEach(() => {
    process.env.NEXT_PUBLIC_EAP_DEV_TOKEN = "Bearer test";
  });
  afterEach(() => {
    process.env.NEXT_PUBLIC_EAP_DEV_TOKEN = original;
    vi.unstubAllGlobals();
  });

  it("calls GET /api/v1/logs with the structured parameters", async () => {
    const fetchMock = vi.fn(async () => new Response(JSON.stringify({ items: [], from: "f", to: "t", limit: 100 }), { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);
    await searchLogs({ run_id: RUN, q: "boom", level: "warn", limit: 100 });
    const url = String((fetchMock.mock.calls[0] as unknown[])[0]);
    expect(url).toMatch(/\/api\/v1\/logs\?/);
    const params = new URL(url).searchParams;
    expect(Object.fromEntries(params)).toEqual({ run_id: RUN, q: "boom", level: "warn", limit: "100" });
  });

  it("throws the API's error detail", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({ detail: "Log search is unavailable" }), { status: 503 })));
    await expect(searchLogs({})).rejects.toMatchObject({ status: 503, message: "Log search is unavailable" });
  });
});

describe("classifyLogsError 404s (follow-up 2)", () => {
  it("tells an unknown agent from an unknown run by the API's detail", () => {
    expect(classifyLogsError(new ApiError(404, "Agent not found"))).toEqual({ kind: "agent_not_found", message: "Agent not found" });
    expect(classifyLogsError(new ApiError(404, "Run not found"))).toEqual({ kind: "not_found", message: "Run not found" });
    expect(classifyLogsError(new ApiError(404, "HTTP 404"))).toEqual({ kind: "not_found", message: "Run not found" });
  });
});
