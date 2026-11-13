import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import React from "react";
import "@testing-library/jest-dom";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { ApiError } from "@/lib/api";
import { getTrace, listTraceRuns, type Trace, type TraceSpan } from "@/lib/traces";
import { SEARCH_DEBOUNCE_MS, searchLogs, type LogItem } from "@/components/logs/logsQuery";
import { TraceView } from "@/components/traces/TraceView";
import LogsPage from "./page";
import { routes } from "@/lib/routes";

const RUN = "11111111-2222-3333-4444-555555555555";
const RUN2 = "99999999-2222-3333-4444-555555555555";
const AGENT = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee";

// A tiny stateful router: replace() rewrites the URL and re-renders subscribers, like Next does.
const nav = vi.hoisted(() => {
  const listeners = new Set<() => void>();
  return {
    search: "",
    replace: undefined as unknown as (url: string) => void,
    listeners,
  };
});
nav.replace = (url: string) => {
  nav.search = url.includes("?") ? url.slice(url.indexOf("?") + 1) : "";
  nav.listeners.forEach((l) => l());
};
const replaceSpy = vi.hoisted(() => vi.fn());

vi.mock("next/navigation", async () => {
  const React = await import("react");
  return {
    useRouter: () => ({ replace: (url: string) => { replaceSpy(url); nav.replace(url); }, push: vi.fn() }),
    useSearchParams: () => {
      const s = React.useSyncExternalStore(
        (cb) => { nav.listeners.add(cb); return () => nav.listeners.delete(cb); },
        () => nav.search,
        () => nav.search,
      );
      return React.useMemo(() => new URLSearchParams(s), [s]);
    },
  };
});
vi.mock("@/lib/api", async (importOriginal) => {
  const original = await importOriginal<typeof import("@/lib/api")>();
  return { ...original, api: { agents: { list: vi.fn(async () => [{ id: AGENT, name: "Support bot" }]) } } };
});
vi.mock("@/lib/workflows", async (importOriginal) => ({ ...(await importOriginal<object>()), listWorkflows: vi.fn(async () => []) }));
vi.mock("@/lib/traces", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/traces")>()),
  listTraceRuns: vi.fn(),
  getTrace: vi.fn(),
}));
vi.mock("@/components/logs/logsQuery", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/components/logs/logsQuery")>()),
  searchLogs: vi.fn(),
}));

function log(message: string, run_id: string | null = RUN, ts = "2026-11-13T10:00:00.123456+00:00"): LogItem {
  return { ts, level: "error", message, service: "eap-worker", trace_id: "t1", span_id: "s1", run_id };
}
const reply = (items: LogItem[], limit = 100) => ({ items, from: "2026-11-13T09:00:00+00:00", to: "2026-11-13T10:00:00+00:00", limit });
function deferred<T>() {
  let resolve!: (v: T) => void;
  let reject!: (e: unknown) => void;
  const promise = new Promise<T>((res, rej) => { resolve = res; reject = rej; });
  return { promise, resolve, reject };
}
const calls = () => vi.mocked(searchLogs).mock.calls.map((c) => c[0]);
const startAt = (qs = "") => { nav.search = qs; return render(<LogsPage />); };

beforeEach(() => {
  nav.search = "";
  replaceSpy.mockReset();
  vi.mocked(searchLogs).mockReset();
  vi.mocked(searchLogs).mockResolvedValue(reply([]));
  vi.mocked(listTraceRuns).mockReset();
  vi.mocked(listTraceRuns).mockResolvedValue({ items: [], next_before: null });
  vi.mocked(getTrace).mockReset();
});
afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

describe("query building and the URL", () => {
  const FROM = "2026-11-13T08:00:00.000Z";
  const TO = "2026-11-13T09:30:00.000Z";
  const cases: Array<[string, Record<string, string>, Record<string, unknown>]> = [
    ["no filters", {}, { limit: 100 }],
    ["q only", { q: "timeout" }, { limit: 100, q: "timeout" }],
    ["level only", { level: "warn" }, { limit: 100, level: "warn" }],
    ["run only", { run_id: RUN }, { limit: 100, run_id: RUN }],
    ["q + level", { q: "x y", level: "error" }, { limit: 100, q: "x y", level: "error" }],
    ["q + level + run", { q: "a", level: "info", run_id: RUN }, { limit: 100, q: "a", level: "info", run_id: RUN }],
    ["custom from/to", { from: FROM, to: TO }, { limit: 100, from: FROM, to: TO }],
    ["custom from only", { from: FROM }, { limit: 100, from: FROM }],
    ["custom to only", { to: TO }, { limit: 100, to: TO }],
    ["everything", { q: "a", level: "fatal", run_id: RUN, from: FROM, to: TO }, { limit: 100, q: "a", level: "fatal", run_id: RUN, from: FROM, to: TO }],
  ];
  it.each(cases)("sends exact params for %s", async (_name, url, expected) => {
    startAt(new URLSearchParams(url).toString());
    await waitFor(() => expect(searchLogs).toHaveBeenCalled());
    expect(calls()).toEqual([expected]);
  });

  it.each([
    ["15m", 15 * 60_000], ["1h", 3_600_000], ["6h", 6 * 3_600_000], ["24h", 24 * 3_600_000], ["7d", 7 * 24 * 3_600_000 - 60_000],
  ])("turns the %s preset into from = now - preset (7d: one minute short of the API maximum) and no to", async (preset, ms) => {
    const now = Date.UTC(2026, 10, 13, 12, 0, 0);
    vi.useFakeTimers({ toFake: ["Date"] });
    vi.setSystemTime(now);
    startAt(`range=${preset}&level=warn`);
    await waitFor(() => expect(searchLogs).toHaveBeenCalled());
    expect(calls()).toEqual([{ limit: 100, level: "warn", from: new Date(now - ms).toISOString() }]);
  });

  it("shows the window the API actually searched (e.g. a clamped 7-day window)", async () => {
    const from = "2026-11-06T12:00:30+00:00";
    const to = "2026-11-13T12:00:30+00:00";
    vi.mocked(searchLogs).mockResolvedValue({ items: [log("one")], from, to, limit: 100 });
    startAt("range=7d");
    const line = await screen.findByTestId("logs-window");
    expect(line).toHaveTextContent(`Searched ${new Date(from).toLocaleString()} to ${new Date(to).toLocaleString()}`);
  });

  it("lets custom from/to override range", async () => {
    startAt(new URLSearchParams({ range: "7d", from: FROM, to: TO }).toString());
    await waitFor(() => expect(searchLogs).toHaveBeenCalled());
    expect(calls()).toEqual([{ limit: 100, from: FROM, to: TO }]);
    expect(screen.getByLabelText("Time range")).toHaveValue("custom");
  });

  it("ignores unknown levels and ranges instead of sending them", async () => {
    startAt("level=loud&range=3y");
    await waitFor(() => expect(searchLogs).toHaveBeenCalled());
    expect(calls()).toEqual([{ limit: 100 }]);
  });

  it("debounces: typing fast sends one request for the final text", async () => {
    vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout"] });
    startAt();
    await act(async () => { await vi.advanceTimersByTimeAsync(0); });
    expect(searchLogs).toHaveBeenCalledTimes(1);
    const box = screen.getByLabelText("Search messages");
    for (const v of ["t", "ti", "tim", "time", "timeo", "timeout"]) {
      fireEvent.change(box, { target: { value: v } });
      await act(async () => { await vi.advanceTimersByTimeAsync(SEARCH_DEBOUNCE_MS - 100); });
    }
    expect(searchLogs).toHaveBeenCalledTimes(1);
    expect(replaceSpy).not.toHaveBeenCalled();
    await act(async () => { await vi.advanceTimersByTimeAsync(SEARCH_DEBOUNCE_MS); });
    expect(replaceSpy).toHaveBeenCalledTimes(1);
    expect(searchLogs).toHaveBeenCalledTimes(2);
    expect(calls()[1]).toEqual({ limit: 100, q: "timeout" });
  });

  it("Enter in the search box searches at once, without waiting for the debounce", async () => {
    startAt();
    await waitFor(() => expect(searchLogs).toHaveBeenCalledTimes(1));
    fireEvent.change(screen.getByLabelText("Search messages"), { target: { value: "now" } });
    fireEvent.submit(screen.getByRole("search", { name: "Log filters" }));
    await waitFor(() => expect(searchLogs).toHaveBeenCalledTimes(2));
    expect(calls()[1]).toEqual({ limit: 100, q: "now" });
  });

  it("changing level and range through the controls re-queries and updates the URL", async () => {
    startAt("q=boom");
    await waitFor(() => expect(searchLogs).toHaveBeenCalledTimes(1));
    fireEvent.change(screen.getByLabelText("Level"), { target: { value: "error" } });
    await waitFor(() => expect(searchLogs).toHaveBeenCalledTimes(2));
    expect(calls()[1]).toEqual({ limit: 100, q: "boom", level: "error" });
    fireEvent.change(screen.getByLabelText("Time range"), { target: { value: "6h" } });
    await waitFor(() => expect(searchLogs).toHaveBeenCalledTimes(3));
    expect(calls()[2]).toMatchObject({ q: "boom", level: "error", from: expect.any(String) });
    expect(new URLSearchParams(nav.search).get("range")).toBe("6h");
  });

  it("restores every control and the same request after a reload", async () => {
    const qs = new URLSearchParams({ q: "boom", level: "warn", agent_id: AGENT, run_id: RUN, range: "24h" }).toString();
    const first = startAt(qs);
    await waitFor(() => expect(searchLogs).toHaveBeenCalled());
    const before = calls()[0];
    first.unmount();
    vi.mocked(searchLogs).mockClear();
    startAt(qs);
    await waitFor(() => expect(searchLogs).toHaveBeenCalled());
    expect(calls()[0]).toMatchObject({ q: "boom", level: "warn", run_id: RUN });
    expect(Object.keys(calls()[0]).sort()).toEqual(Object.keys(before).sort());
    expect(screen.getByLabelText("Search messages")).toHaveValue("boom");
    expect(screen.getByLabelText("Level")).toHaveValue("warn");
    expect(screen.getByLabelText("Run")).toHaveValue(RUN);
    expect(screen.getByLabelText("Time range")).toHaveValue("24h");
    await waitFor(() => expect(screen.getByLabelText("Agent")).toHaveValue(AGENT));
  });

  it("Clear filters empties the URL and every control and queries with defaults", async () => {
    startAt(new URLSearchParams({ q: "boom", level: "warn", run_id: RUN, range: "1h" }).toString());
    await waitFor(() => expect(searchLogs).toHaveBeenCalledTimes(1));
    fireEvent.click(screen.getByRole("button", { name: "Clear filters" }));
    expect(replaceSpy).toHaveBeenLastCalledWith(routes.logs());
    await waitFor(() => expect(searchLogs).toHaveBeenCalledTimes(2));
    expect(calls()[1]).toEqual({ limit: 100 });
    expect(screen.getByLabelText("Search messages")).toHaveValue("");
    expect(screen.getByLabelText("Level")).toHaveValue("");
    expect(screen.getByLabelText("Run")).toHaveValue("");
    expect(screen.queryByRole("button", { name: "Clear filters" })).toBeNull();
  });
});

describe("states", () => {
  it("shows the empty state with its status announcement", async () => {
    startAt();
    expect(await screen.findByTestId("logs-empty")).toHaveTextContent("No logs in the last hour");
    expect(screen.getByTestId("logs-status")).toHaveTextContent("0 log records");
  });

  it("503 is 'Loki unavailable' with the API's message, not a generic error", async () => {
    vi.mocked(searchLogs).mockRejectedValue(new ApiError(503, "Log search is not configured"));
    startAt();
    const box = await screen.findByTestId("logs-unavailable");
    expect(box).toHaveTextContent("Loki unavailable");
    expect(box).toHaveTextContent("Log search is not configured");
    expect(screen.queryByTestId("logs-error")).toBeNull();
    expect(screen.queryByTestId("log-row")).toBeNull();
  });

  it.each([[404, "Run not found"], [422, "bad level"], [502, "Log search failed"]])("%i shows logs-error, not the Loki state", async (status, text) => {
    vi.mocked(searchLogs).mockRejectedValue(new ApiError(status, status === 422 ? "bad level" : "x"));
    startAt();
    const box = await screen.findByTestId("logs-error");
    expect(box).toHaveTextContent("Could not load logs");
    expect(box).toHaveTextContent(text);
    expect(screen.queryByTestId("logs-unavailable")).toBeNull();
  });

  it("a network failure shows logs-error", async () => {
    vi.mocked(searchLogs).mockRejectedValue(new TypeError("fetch failed"));
    startAt();
    expect(await screen.findByTestId("logs-error")).toHaveTextContent("Could not reach the API");
  });

  it.each([
    ["run_id=nope", "Run id must be a UUID"],
    ["agent_id=nope", "Unknown agent"],
    ["from=garbage", "'From' is not a valid time"],
    ["to=garbage", "'To' is not a valid time"],
    ["from=2026-11-13T10:00:00Z&to=2026-11-13T09:00:00Z", "'From' must be before 'To'"],
    ["from=2026-11-01T00:00:00Z&to=2026-11-13T00:00:00Z", "The time range may span at most 7 days"],
  ])("%s shows an inline problem and sends no request", async (qs, message) => {
    startAt(qs);
    expect(await screen.findByTestId("logs-filter-problem")).toHaveTextContent(message);
    await act(async () => { await new Promise((r) => setTimeout(r, 30)); });
    expect(searchLogs).not.toHaveBeenCalled();
    expect(screen.queryByTestId("logs-error")).toBeNull();
  });

  it("fixing an invalid run id then searches", async () => {
    startAt("run_id=nope");
    await screen.findByTestId("logs-filter-problem");
    fireEvent.change(screen.getByLabelText("Run"), { target: { value: RUN } });
    await waitFor(() => expect(searchLogs).toHaveBeenCalledTimes(1));
    expect(calls()[0]).toEqual({ limit: 100, run_id: RUN });
    expect(screen.queryByTestId("logs-filter-problem")).toBeNull();
  });

  it("Retry after a failure re-sends the same query and shows the records", async () => {
    vi.mocked(searchLogs).mockRejectedValueOnce(new ApiError(503, "down")).mockResolvedValueOnce(reply([log("recovered")]));
    startAt("q=boom");
    await screen.findByTestId("logs-unavailable");
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(await screen.findByText("recovered")).toBeInTheDocument();
    expect(calls()).toEqual([{ limit: 100, q: "boom" }, { limit: 100, q: "boom" }]);
    expect(screen.queryByTestId("logs-unavailable")).toBeNull();
  });
});

describe("stale responses and paging", () => {
  it("drops an older query's response that arrives after a newer one", async () => {
    const slow = deferred<ReturnType<typeof reply>>();
    vi.mocked(searchLogs).mockReturnValueOnce(slow.promise).mockResolvedValueOnce(reply([log("from newer")]));
    startAt("q=old");
    await waitFor(() => expect(searchLogs).toHaveBeenCalledTimes(1));
    act(() => nav.replace(routes.logs({ q: "new" })));
    expect(await screen.findByText("from newer")).toBeInTheDocument();
    await act(async () => { slow.resolve(reply([log("from older")])); });
    expect(screen.queryByText("from older")).toBeNull();
    expect(screen.getByText("from newer")).toBeInTheDocument();
    expect(screen.getByTestId("logs-status")).toHaveTextContent("1 log record");
  });

  it("drops an older query's error that arrives after a newer success", async () => {
    const slow = deferred<ReturnType<typeof reply>>();
    vi.mocked(searchLogs).mockReturnValueOnce(slow.promise).mockResolvedValueOnce(reply([log("fresh")]));
    startAt("q=old");
    await waitFor(() => expect(searchLogs).toHaveBeenCalledTimes(1));
    act(() => nav.replace(routes.logs({ q: "new" })));
    await screen.findByText("fresh");
    await act(async () => { slow.reject(new ApiError(503, "late")); });
    expect(screen.queryByTestId("logs-unavailable")).toBeNull();
    expect(screen.getByText("fresh")).toBeInTheDocument();
  });

  const page = (n: number, hour = 9) =>
    Array.from({ length: n }, (_, i) => log(`m${hour}-${i}`, RUN, `2026-11-13T${String(hour).padStart(2, "0")}:${String(59 - Math.floor(i / 60)).padStart(2, "0")}:${String(59 - (i % 60)).padStart(2, "0")}.000100+00:00`));

  it("Load older asks for to = oldest shown + 1 microsecond, from = the first window's from, and de-duplicates", async () => {
    const first = page(100);
    const oldestTs = first[99].ts;
    vi.mocked(searchLogs)
      .mockResolvedValueOnce(reply(first))
      .mockResolvedValueOnce(reply([first[99], log("older one", RUN, "2026-11-13T08:00:00.000000+00:00")]));
    startAt("level=error");
    fireEvent.click(await screen.findByRole("button", { name: "Load older" }));
    expect(await screen.findByText("older one")).toBeInTheDocument();
    const iso = new Date(Date.parse(oldestTs)).toISOString().replace(/\.\d+Z$/, "");
    expect(calls()[1]).toMatchObject({ level: "error", from: "2026-11-13T09:00:00+00:00", to: `${iso}.000101Z` });
    expect(screen.getAllByTestId("log-row")).toHaveLength(101);
    expect(screen.getByTestId("logs-status")).toHaveTextContent("101 log records");
  });

  it("stops offering Load older when a page is short", async () => {
    vi.mocked(searchLogs).mockResolvedValueOnce(reply(page(100))).mockResolvedValueOnce(reply([log("last", RUN, "2026-11-13T08:00:00+00:00")]));
    startAt();
    fireEvent.click(await screen.findByRole("button", { name: "Load older" }));
    await screen.findByText("last");
    expect(screen.queryByRole("button", { name: "Load older" })).toBeNull();
  });

  it("stops when an older page adds nothing new, instead of looping", async () => {
    const first = page(100);
    vi.mocked(searchLogs).mockResolvedValueOnce(reply(first)).mockResolvedValueOnce(reply(first));
    startAt();
    fireEvent.click(await screen.findByRole("button", { name: "Load older" }));
    await waitFor(() => expect(searchLogs).toHaveBeenCalledTimes(2));
    await waitFor(() => expect(screen.queryByRole("button", { name: "Load older" })).toBeNull());
    expect(screen.getAllByTestId("log-row")).toHaveLength(100);
  });

  it("drops a Load older response that arrives after the filters changed", async () => {
    const olderResponse = deferred<ReturnType<typeof reply>>();
    vi.mocked(searchLogs)
      .mockResolvedValueOnce(reply(page(100)))
      .mockReturnValueOnce(olderResponse.promise)
      .mockResolvedValueOnce(reply([log("new filter result")]));
    startAt();
    fireEvent.click(await screen.findByRole("button", { name: "Load older" }));
    await waitFor(() => expect(searchLogs).toHaveBeenCalledTimes(2));
    act(() => nav.replace(routes.logs({ level: "fatal" })));
    await screen.findByText("new filter result");
    await act(async () => { olderResponse.resolve(reply([log("stale older")])); });
    expect(screen.queryByText("stale older")).toBeNull();
    expect(screen.getAllByTestId("log-row")).toHaveLength(1);
  });

  it("shows a Load older failure without losing the records", async () => {
    vi.mocked(searchLogs).mockResolvedValueOnce(reply(page(100))).mockRejectedValueOnce(new ApiError(503, "Log search is unavailable"));
    startAt();
    fireEvent.click(await screen.findByRole("button", { name: "Load older" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Log search is unavailable");
    expect(screen.getAllByTestId("log-row")).toHaveLength(100);
  });
});

describe("agent filter", () => {
  const runRow = (run_id: string) => ({ run_id, created_at: "2026-11-13T09:59:00Z", display_status: "completed" }) as never;

  it("shows only the records of that agent's runs, with the note, and sends no agent param", async () => {
    vi.mocked(listTraceRuns).mockResolvedValue({ items: [runRow(RUN)], next_before: null });
    vi.mocked(searchLogs).mockResolvedValue(reply([log("mine"), log("theirs", RUN2), log("no run", null)]));
    startAt(`agent_id=${AGENT}`);
    expect(await screen.findByText("mine")).toBeInTheDocument();
    expect(screen.queryByText("theirs")).toBeNull();
    expect(screen.queryByText("no run")).toBeNull();
    expect(screen.getAllByTestId("log-row")).toHaveLength(1);
    expect(screen.getByTestId("logs-agent-note")).toHaveTextContent(/most recent run/);
    expect(calls()[0]).toEqual({ limit: 100 });
    expect(vi.mocked(listTraceRuns).mock.calls.some(([f, o]) => f.agent_id === AGENT && o?.limit === 200)).toBe(true);
  });

  it("is not applied when a run is picked (all of that run's logs show)", async () => {
    vi.mocked(listTraceRuns).mockResolvedValue({ items: [runRow(RUN)], next_before: null });
    vi.mocked(searchLogs).mockResolvedValue(reply([log("x", RUN2)]));
    startAt(`agent_id=${AGENT}&run_id=${RUN2}`);
    expect(await screen.findByText("x")).toBeInTheDocument();
    expect(screen.queryByTestId("logs-agent-note")).toBeNull();
  });

  it("changing the agent re-filters without a new logs request and shows no note when none selected", async () => {
    vi.mocked(listTraceRuns).mockResolvedValue({ items: [runRow(RUN)], next_before: null });
    vi.mocked(searchLogs).mockResolvedValue(reply([log("mine"), log("theirs", RUN2)]));
    startAt();
    await screen.findByText("theirs");
    expect(screen.queryByTestId("logs-agent-note")).toBeNull();
    fireEvent.change(await screen.findByLabelText("Agent"), { target: { value: AGENT } });
    await waitFor(() => expect(screen.queryByText("theirs")).toBeNull());
    expect(screen.getByText("mine")).toBeInTheDocument();
    expect(searchLogs).toHaveBeenCalledTimes(1);
    expect(screen.getByTestId("logs-agent-note")).toBeVisible();
  });

  it("an agent with no runs shows the empty state, not other agents' records", async () => {
    vi.mocked(searchLogs).mockResolvedValue(reply([log("theirs", RUN2)]));
    startAt(`agent_id=${AGENT}`);
    expect(await screen.findByTestId("logs-empty")).toBeInTheDocument();
    expect(screen.queryByText("theirs")).toBeNull();
  });
});

const T = (s: number) => new Date(Date.UTC(2026, 10, 13, 10, 0, s)).toISOString();
function span(id: string, parent: string | null, kind: TraceSpan["kind"], name: string, start: number, end: number | null, attributes: TraceSpan["attributes"] = {}): TraceSpan {
  return { id, parent_id: parent, kind, name, start: T(start), end: end === null ? null : T(end), status: "ok", attributes, input: null, output: null, truncated: {} };
}
function fixtureTrace(spans: TraceSpan[]): Trace {
  return {
    run: {
      run_id: RUN, kind: "agent", agent_id: "agent-1", version: 1, tool_id: null, workflow_id: null, workflow_version: null,
      status: "completed", display_status: "completed", input: "hi", output: "ok", error: null, heartbeat_at: null,
      created_at: T(0), updated_at: T(10), trace_id: null, conversation_id: null, turn: null,
    } as Trace["run"],
    spans,
    totals: {
      runs: 1, model_requests: 1, tool_calls: 1, input_tokens: null, output_tokens: null, cache_read_tokens: null, cache_write_tokens: null,
      cost_usd: null, unpriced_requests: 0, unrecorded_requests: 0, duration_ms: 10_000, status: "completed",
    },
    truncated: { spans: false, events: false, child_runs: false, payload_fields: 0 },
    limits: { max_spans: 2000, max_events_per_run: 10_000, max_child_runs: 500, field_max_bytes: 8192, payload_budget_bytes: 2097152 },
  };
}
const rootSpan = span("run", null, "run", "agent run", 0, 10, { run_kind: "agent", agent_id: "agent-1", agent_version: 1 });
const modelSpan = span("m0", "run", "model_request", "test:model", 2, 5, { model_id: "test:model", model_request: 0 });
const toolSpan = span("t0", "run", "tool_call", "lookup", 6, 8, { tool_id: "lookup" });
const logAt = (message: string, s: number) => ({ ...log(message), ts: T(s) });

/** A fake GET /logs over fixed records: from inclusive, to exclusive, newest first, newest `limit`. */
const RECORDS = [logAt("before the model", 1), logAt("inside the model", 3), logAt("inside the tool", 7), logAt("after everything", 9)];
function fakeServer() {
  vi.mocked(searchLogs).mockImplementation(async (p) => {
    const lo = p.from ? Date.parse(p.from) : -Infinity;
    const hi = p.to ? Date.parse(p.to) : Infinity;
    const hit = RECORDS.filter((r) => p.run_id === RUN && Date.parse(r.ts) >= lo && Date.parse(r.ts) < hi);
    return reply(hit.reverse().slice(0, p.limit ?? 100), p.limit ?? 100);
  });
}
const panelMessages = () => within(screen.getByTestId("trace-logs-panel")).queryAllByTestId("log-row").map((r) => r.querySelector("pre")?.textContent);

describe("span window", () => {
  it("selecting a span queries from = its start and to = its end + 1 ms, and shows that span's records", async () => {
    vi.mocked(getTrace).mockResolvedValue(fixtureTrace([rootSpan, modelSpan, toolSpan]));
    fakeServer();
    render(<TraceView runId={RUN} />);
    const rows = await screen.findAllByTestId("trace-span-row");
    await waitFor(() => expect(panelMessages()).toHaveLength(4));
    fireEvent.click(rows[1]);
    await waitFor(() => expect(panelMessages()).toEqual(["inside the model"]));
    expect(calls().at(-1)).toEqual({ run_id: RUN, limit: 500, from: "2026-11-13T10:00:02.000000Z", to: "2026-11-13T10:00:05.001000Z" });
    expect(screen.getByTestId("trace-logs-scope")).toHaveTextContent("Model request #0: test:model");
    fireEvent.click(rows[2]);
    await waitFor(() => expect(panelMessages()).toEqual(["inside the tool"]));
    expect(calls().at(-1)).toEqual({ run_id: RUN, limit: 500, from: "2026-11-13T10:00:06.000000Z", to: "2026-11-13T10:00:08.001000Z" });
  });

  it("includes a record logged exactly at the span's end", async () => {
    RECORDS.push(logAt("at the end", 5));
    try {
      vi.mocked(getTrace).mockResolvedValue(fixtureTrace([rootSpan, modelSpan]));
      fakeServer();
      render(<TraceView runId={RUN} />);
      fireEvent.click((await screen.findAllByTestId("trace-span-row"))[1]);
      await waitFor(() => expect(panelMessages()).toEqual(["inside the model", "at the end"]));
    } finally {
      RECORDS.pop();
    }
  });

  it("an open span runs to now: from its start, no upper bound", async () => {
    const open = span("t0", "run", "tool_call", "lookup", 6, null, { tool_id: "lookup" });
    vi.mocked(getTrace).mockResolvedValue(fixtureTrace([rootSpan, open]));
    fakeServer();
    render(<TraceView runId={RUN} />);
    fireEvent.click((await screen.findAllByTestId("trace-span-row"))[1]);
    await waitFor(() => expect(panelMessages()).toEqual(["inside the tool", "after everything"]));
    expect(calls().at(-1)).toEqual({ run_id: RUN, limit: 500, from: "2026-11-13T10:00:06.000000Z" });
  });

  it("'Show all run logs' switches to the run's lifetime window (created_at - 1 min to its end + 5 s) and back", async () => {
    // The exact `to` depends on "now" (a finish + 5 s still in the future is left to the API): freeze it.
    vi.useFakeTimers({ toFake: ["Date"] });
    vi.setSystemTime(new Date("2026-11-13T12:00:00Z"));
    vi.mocked(getTrace).mockResolvedValue(fixtureTrace([rootSpan, modelSpan]));
    fakeServer();
    render(<TraceView runId={RUN} />);
    fireEvent.click((await screen.findAllByTestId("trace-span-row"))[1]);
    await waitFor(() => expect(panelMessages()).toEqual(["inside the model"]));
    fireEvent.click(screen.getByRole("button", { name: "Show all run logs" }));
    await waitFor(() => expect(panelMessages()).toHaveLength(4));
    // Review fix: an explicit lifetime window, never the API default (which runs to now).
    expect(calls().at(-1)).toEqual({ run_id: RUN, limit: 500, from: "2026-11-13T09:59:00.000000Z", to: "2026-11-13T10:00:15.000000Z" });
    expect(panelMessages()).toEqual(["before the model", "inside the model", "inside the tool", "after everything"]);
    fireEvent.click(screen.getByRole("button", { name: "Only the selected span" }));
    await waitFor(() => expect(panelMessages()).toEqual(["inside the model"]));
  });

  it("a span with no records says so, and the whole run is one click away", async () => {
    const quiet = span("q", "run", "tool_call", "quiet", 4, 5, { tool_id: "quiet" });
    vi.mocked(getTrace).mockResolvedValue(fixtureTrace([rootSpan, quiet]));
    fakeServer();
    render(<TraceView runId={RUN} />);
    fireEvent.click((await screen.findAllByTestId("trace-span-row"))[1]);
    expect(await screen.findByTestId("trace-logs-empty")).toHaveTextContent("No logs during this span.");
    fireEvent.click(within(screen.getByTestId("trace-logs-empty")).getByRole("button", { name: "Show all run logs" }));
    await waitFor(() => expect(panelMessages()).toHaveLength(4));
  });

  it("Loki unavailable inside the trace view leaves the trace usable", async () => {
    vi.mocked(getTrace).mockResolvedValue(fixtureTrace([rootSpan, modelSpan]));
    vi.mocked(searchLogs).mockRejectedValue(new ApiError(503, "Log search is not configured"));
    render(<TraceView runId={RUN} />);
    expect(await screen.findByTestId("logs-unavailable")).toHaveTextContent("Log search is not configured");
    expect(screen.getAllByTestId("trace-span-row").length).toBe(2);
  });

  it("a stale window response is dropped after selecting another span", async () => {
    vi.mocked(getTrace).mockResolvedValue(fixtureTrace([rootSpan, modelSpan, toolSpan]));
    const slow = deferred<ReturnType<typeof reply>>();
    vi.mocked(searchLogs).mockReturnValueOnce(slow.promise);
    fakeServer();
    render(<TraceView runId={RUN} />);
    const rows = await screen.findAllByTestId("trace-span-row");
    fireEvent.click(rows[2]);
    await waitFor(() => expect(panelMessages()).toEqual(["inside the tool"]));
    await act(async () => { slow.resolve(reply([logAt("stale root result", 4)])); });
    expect(panelMessages()).toEqual(["inside the tool"]);
  });
});

describe("acceptance: log line to trace and span to logs", () => {
  it("from a log line, the link opens that run's trace; lines outside a run have none", async () => {
    vi.mocked(searchLogs).mockResolvedValue(reply([log("one", RUN), log("two", RUN2, "2026-11-13T09:59:00+00:00"), log("orphan", null, "2026-11-13T09:58:00+00:00")]));
    startAt();
    await screen.findByText("one");
    const links = screen.getAllByRole("link", { name: /Open trace of run/ });
    expect(links.map((l) => l.getAttribute("href"))).toEqual([routes.trace(RUN), routes.trace(RUN2)]);
    const orphanRow = screen.getAllByTestId("log-row")[2];
    expect(within(orphanRow).queryByRole("link")).toBeNull();
  });

  it("from a span, the panel offers a link to the same run's Logs page", async () => {
    vi.mocked(getTrace).mockResolvedValue(fixtureTrace([rootSpan, modelSpan]));
    fakeServer();
    render(<TraceView runId={RUN} />);
    await screen.findAllByTestId("trace-span-row");
    expect(screen.getByRole("link", { name: "Search in Logs" })).toHaveAttribute("href", `${routes.logs()}?run_id=${RUN}`);
  });

  it("the Logs page link from a trace loads that run's logs with the run filter set", async () => {
    startAt(`run_id=${RUN}`);
    await waitFor(() => expect(searchLogs).toHaveBeenCalled());
    expect(calls()[0]).toEqual({ limit: 100, run_id: RUN });
    expect(screen.getByLabelText("Run")).toHaveValue(RUN);
  });
});

describe("accessibility", () => {
  it("every filter control has an accessible name and is keyboard reachable", async () => {
    startAt("range=1h");
    await waitFor(() => expect(searchLogs).toHaveBeenCalled());
    const form = screen.getByRole("search", { name: "Log filters" });
    for (const name of ["Search messages", "Level", "Agent", "Run", "Time range"]) {
      const el = within(form).getByLabelText(name) as HTMLElement;
      expect(el.tabIndex).toBeGreaterThanOrEqual(0);
      expect(el).not.toBeDisabled();
      el.focus();
      expect(el).toHaveFocus();
    }
    fireEvent.change(screen.getByLabelText("Time range"), { target: { value: "custom" } });
    expect(await screen.findByLabelText("From")).toHaveAttribute("type", "datetime-local");
    expect(screen.getByLabelText("To")).toHaveAttribute("type", "datetime-local");
    for (const name of ["Refresh", "Clear filters"]) {
      const b = screen.getByRole("button", { name });
      expect(b.tabIndex).toBeGreaterThanOrEqual(0);
    }
  });

  it("the live region announces counts, singular and plural, and the searching state", async () => {
    const slow = deferred<ReturnType<typeof reply>>();
    vi.mocked(searchLogs).mockReturnValueOnce(slow.promise);
    startAt();
    const status = screen.getByTestId("logs-status");
    expect(status).toHaveAttribute("aria-live", "polite");
    await waitFor(() => expect(status).toHaveTextContent("Searching logs"));
    await act(async () => { slow.resolve(reply([log("a")])); });
    expect(status).toHaveTextContent("1 log record");
    expect(status).not.toHaveTextContent("1 log records");
    vi.mocked(searchLogs).mockResolvedValueOnce(reply([log("a"), log("b", RUN, "2026-11-13T09:00:00+00:00")]));
    fireEvent.click(screen.getByRole("button", { name: "Refresh" }));
    await waitFor(() => expect(status).toHaveTextContent("2 log records"));
  });

  it("announces that older records exist when the page is full", async () => {
    const full = Array.from({ length: 100 }, (_, i) => log(`m${i}`, RUN, `2026-11-13T09:${String(59 - Math.floor(i / 60)).padStart(2, "0")}:${String(59 - (i % 60)).padStart(2, "0")}+00:00`));
    vi.mocked(searchLogs).mockResolvedValue(reply(full));
    startAt();
    await waitFor(() => expect(screen.getByTestId("logs-status")).toHaveTextContent("100 log records, older records available"));
  });

  it("the table has a caption and column headers, and an invalid run is flagged on the field", async () => {
    vi.mocked(searchLogs).mockResolvedValue(reply([log("a")]));
    const first = startAt();
    await screen.findByText("a");
    const table = screen.getByRole("table", { name: "Log records, newest first" });
    expect(within(table).getAllByRole("columnheader").map((h) => h.textContent)).toEqual(["Time", "Level", "Service", "Message", "Trace"]);
    first.unmount();
    startAt("run_id=bad");
    expect(await screen.findByRole("alert")).toHaveTextContent("Run id must be a UUID");
    expect(screen.getByLabelText("Run")).toHaveAttribute("aria-invalid", "true");
  });

  it("the trace panel's live region announces the scope and count, and its toggle exposes its state", async () => {
    vi.mocked(getTrace).mockResolvedValue(fixtureTrace([rootSpan, modelSpan]));
    fakeServer();
    render(<TraceView runId={RUN} />);
    fireEvent.click((await screen.findAllByTestId("trace-span-row"))[1]);
    const panel = screen.getByTestId("trace-logs-panel");
    await waitFor(() => expect(panel.querySelector("[aria-live=polite]")).toHaveTextContent("1 log record during Model request #0: test:model"));
    const toggle = within(panel).getByRole("button", { name: "Show all run logs" });
    expect(toggle).toHaveAttribute("aria-pressed", "false");
    fireEvent.click(toggle);
    expect(within(panel).getByRole("button", { name: "Only the selected span" })).toHaveAttribute("aria-pressed", "true");
    await waitFor(() => expect(panelMessages()).toHaveLength(4));
  });
});
