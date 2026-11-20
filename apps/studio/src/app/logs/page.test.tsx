import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import React from "react";
import "@testing-library/jest-dom";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { ApiError } from "@/lib/api";
import { listTraceRuns } from "@/lib/traces";
import { getRun } from "@/lib/runs";
import { SEARCH_DEBOUNCE_MS, searchLogs, type LogItem } from "@/components/logs/logsQuery";
import LogsPage from "./page";
import { routes } from "@/lib/routes";
import { getSelectOption } from "@/test/select";

const RUN = "11111111-2222-3333-4444-555555555555";
const OTHER_RUN = "99999999-2222-3333-4444-555555555555";
const AGENT = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee";

let currentParams = new URLSearchParams();
const replace = vi.fn();
const push = vi.fn();

vi.mock("next/navigation", () => ({
  usePathname: () => "/logs",
  useRouter: () => ({ replace, push }),
  useSearchParams: () => currentParams,
}));

vi.mock("@/lib/api", async (importOriginal) => {
  const original = await importOriginal<typeof import("@/lib/api")>();
  return { ...original, api: { ...original.api, agents: { ...original.api.agents, list: vi.fn(async () => [{ id: AGENT, name: "Support bot" }]) } } };
});

vi.mock("@/lib/traces", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/traces")>()),
  listTraceRuns: vi.fn(async () => ({ items: [], next_before: null })),
}));

vi.mock("@/lib/runs", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/lib/runs")>()),
  getRun: vi.fn(async () => {
    throw new Error("not mocked");
  }),
}));

vi.mock("@/components/logs/logsQuery", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/components/logs/logsQuery")>()),
  searchLogs: vi.fn(),
}));

function log(message: string, run_id: string | null = RUN, ts = "2026-11-13T10:00:00.123456+00:00"): LogItem {
  return { ts, level: "error", message, service: "eap-worker", trace_id: "t1", span_id: "s1", run_id };
}

const response = (items: LogItem[]) => ({ items, from: "2026-11-13T09:00:00+00:00", to: "2026-11-13T10:00:00+00:00", limit: 100 });

describe("Logs page", () => {
  beforeEach(() => {
    currentParams = new URLSearchParams();
    replace.mockReset();
    push.mockReset();
    vi.mocked(searchLogs).mockReset();
    vi.mocked(listTraceRuns).mockReset();
    vi.mocked(listTraceRuns).mockResolvedValue({ items: [], next_before: null });
    vi.mocked(getRun).mockReset();
    vi.mocked(getRun).mockRejectedValue(new Error("not mocked"));
  });
  afterEach(() => vi.useRealTimers());

  it("shows records with time, level, service, message and a link to the run's trace", async () => {
    vi.mocked(searchLogs).mockResolvedValue(response([log("model call failed"), log("outside a run", null)]));
    render(<LogsPage />);
    expect(await screen.findByText("model call failed")).toBeInTheDocument();
    const rows = screen.getAllByTestId("log-row");
    expect(rows).toHaveLength(2);
    expect(rows[0]).toHaveTextContent("error");
    expect(rows[0]).toHaveTextContent("eap-worker");
    expect(rows[0].querySelector("time")).toHaveAttribute("dateTime", "2026-11-13T10:00:00.123456+00:00");
    expect(screen.getByRole("link", { name: `Open trace of run ${RUN}` })).toHaveAttribute("href", routes.trace(RUN));
    expect(screen.getAllByRole("link", { name: /Open trace/ })).toHaveLength(1);
  });

  it("sends the URL filters to GET /logs and never the agent", async () => {
    currentParams = new URLSearchParams({ q: "boom", level: "warn", run_id: RUN, from: "2026-11-13T09:00:00.000Z", to: "2026-11-13T10:00:00.000Z" });
    vi.mocked(searchLogs).mockResolvedValue(response([]));
    render(<LogsPage />);
    await waitFor(() => expect(searchLogs).toHaveBeenCalled());
    expect(vi.mocked(searchLogs).mock.calls[0][0]).toEqual({
      limit: 100, run_id: RUN, q: "boom", level: "warn", from: "2026-11-13T09:00:00.000Z", to: "2026-11-13T10:00:00.000Z",
    });
    expect(screen.getByLabelText("Search messages")).toHaveValue("boom");
  });

  it("debounces the search box into the URL", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    vi.mocked(searchLogs).mockResolvedValue(response([]));
    render(<LogsPage />);
    const box = screen.getByLabelText("Search messages");
    fireEvent.change(box, { target: { value: "ti" } });
    fireEvent.change(box, { target: { value: "time" } });
    act(() => vi.advanceTimersByTime(SEARCH_DEBOUNCE_MS - 50));
    expect(replace).not.toHaveBeenCalled();
    act(() => vi.advanceTimersByTime(60));
    expect(replace).toHaveBeenCalledTimes(1);
    expect(replace).toHaveBeenCalledWith(routes.logs({ q: "time" }), { scroll: false });
  });

  it("writes the level and time range to the URL", async () => {
    vi.mocked(searchLogs).mockResolvedValue(response([]));
    render(<LogsPage />);
    fireEvent.change(screen.getByLabelText("Level"), { target: { value: "error" } });
    expect(replace).toHaveBeenLastCalledWith(routes.logs({ level: "error" }), { scroll: false });
    fireEvent.change(screen.getByLabelText("Time range"), { target: { value: "24h" } });
    // The level chosen first is kept although the URL has not re-rendered yet.
    expect(replace).toHaveBeenLastCalledWith(routes.logs({ level: "error", range: "24h" }), { scroll: false });
  });

  it("shows the Loki-unavailable state with a retry", async () => {
    vi.mocked(searchLogs).mockRejectedValueOnce(new ApiError(503, "Log search is unavailable")).mockResolvedValueOnce(response([log("back")]));
    render(<LogsPage />);
    const state = await screen.findByTestId("logs-unavailable");
    expect(state).toHaveTextContent("Loki unavailable");
    expect(state).toHaveTextContent("Log search is unavailable");
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(await screen.findByText("back")).toBeInTheDocument();
  });

  it("shows an empty state", async () => {
    vi.mocked(searchLogs).mockResolvedValue(response([]));
    currentParams = new URLSearchParams({ q: "nothing" });
    render(<LogsPage />);
    expect(await screen.findByTestId("logs-empty")).toHaveTextContent("No logs match these filters");
  });

  it("does not query with an invalid run id", async () => {
    currentParams = new URLSearchParams({ run_id: "not-a-uuid" });
    render(<LogsPage />);
    expect(await screen.findByTestId("logs-filter-problem")).toHaveTextContent("Run id must be a UUID");
    expect(searchLogs).not.toHaveBeenCalled();
  });

  it("filters by agent through the API's agent_id", async () => {
    currentParams = new URLSearchParams({ agent_id: AGENT });
    vi.mocked(searchLogs).mockResolvedValue({ ...response([log("mine")]), agent_runs: 1, agent_runs_limited: false });
    render(<LogsPage />);
    expect(await screen.findByText("mine")).toBeInTheDocument();
    expect(vi.mocked(searchLogs).mock.calls[0][0]).toMatchObject({ agent_id: AGENT });
    expect(screen.getByTestId("logs-agent-note")).toHaveTextContent("Searched this agent's 1 run in this time range.");
  });

  it("loads older records", async () => {
    const full = Array.from({ length: 100 }, (_, i) => log(`m${i}`, RUN, `2026-11-13T09:${String(59 - (i % 60)).padStart(2, "0")}:${String(i % 60).padStart(2, "0")}+00:00`));
    vi.mocked(searchLogs).mockResolvedValueOnce(response(full)).mockResolvedValueOnce(response([log("older", RUN, "2026-11-13T09:00:00+00:00")]));
    render(<LogsPage />);
    fireEvent.click(await screen.findByRole("button", { name: "Load older" }));
    expect(await screen.findByText("older")).toBeInTheDocument();
    expect(vi.mocked(searchLogs).mock.calls[1][0]).toMatchObject({ from: "2026-11-13T09:00:00+00:00" });
    expect(screen.queryByRole("button", { name: "Load older" })).toBeNull();
  });

  it("keeps a level chosen while the search debounce is pending", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    vi.mocked(searchLogs).mockResolvedValue(response([]));
    render(<LogsPage />);
    fireEvent.change(screen.getByLabelText("Search messages"), { target: { value: "timeout" } });
    act(() => vi.advanceTimersByTime(100));
    fireEvent.change(screen.getByLabelText("Level"), { target: { value: "error" } });
    expect(replace).toHaveBeenLastCalledWith(routes.logs({ level: "error" }), { scroll: false });
    act(() => vi.advanceTimersByTime(SEARCH_DEBOUNCE_MS));
    expect(replace).toHaveBeenLastCalledWith(routes.logs({ q: "timeout", level: "error" }), { scroll: false });
  });

  it("keeps the agent and range when the debounced text lands", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    vi.mocked(searchLogs).mockResolvedValue(response([]));
    render(<LogsPage />);
    await screen.findByRole("option", { name: "Support bot" });
    fireEvent.change(screen.getByLabelText("Search messages"), { target: { value: "x" } });
    fireEvent.change(screen.getByLabelText("Agent"), { target: { value: AGENT } });
    fireEvent.change(screen.getByLabelText("Time range"), { target: { value: "6h" } });
    act(() => vi.advanceTimersByTime(SEARCH_DEBOUNCE_MS + 10));
    expect(replace).toHaveBeenLastCalledWith(`${routes.logs()}?q=x&agent_id=${AGENT}&range=6h`, { scroll: false });
  });

  it("Enter with search text and a partial run id writes both in one URL change", async () => {
    vi.mocked(searchLogs).mockResolvedValue(response([]));
    render(<LogsPage />);
    fireEvent.change(screen.getByLabelText("Search messages"), { target: { value: "boom" } });
    fireEvent.change(screen.getByLabelText("Run"), { target: { value: "1111" } });
    fireEvent.submit(screen.getByRole("search", { name: "Log filters" }));
    expect(replace).toHaveBeenCalledTimes(1);
    expect(replace).toHaveBeenCalledWith(routes.logs({ q: "boom", run_id: "1111" }), { scroll: false });
  });

  it("blur-then-click race: a run id committed on blur is kept when the click opens the trace Sheet", async () => {
    vi.mocked(searchLogs).mockResolvedValue(response([log("model call failed")]));
    render(<LogsPage />);
    const link = await screen.findByRole("link", { name: `Open trace of run ${RUN}` });
    fireEvent.change(screen.getByLabelText("Run"), { target: { value: "1111" } });
    // The click that opens the Sheet first blurs the field, which commits the partial id; the router
    // has not put it in the URL yet (currentParams is still empty), so the Sheet must build on it,
    // and REPLACE (Next drops a pending replace under a push: test/pendingFilterWrite.test.tsx).
    fireEvent.blur(screen.getByLabelText("Run"));
    expect(replace).toHaveBeenLastCalledWith(routes.logs({ run_id: "1111" }), { scroll: false });
    fireEvent.click(link);
    expect(push).not.toHaveBeenCalled();
    const url = new URL(replace.mock.calls[replace.mock.calls.length - 1][0] as string, "http://x");
    expect(url.pathname).toBe("/logs");
    expect(url.searchParams.get("run_id")).toBe("1111");
    expect(url.searchParams.get("trace")).toBe(RUN);
  });

  it("without a pending filter write, opening a trace builds on the URL as it is", async () => {
    currentParams = new URLSearchParams({ level: "error" });
    vi.mocked(searchLogs).mockResolvedValue(response([log("model call failed")]));
    render(<LogsPage />);
    fireEvent.click(await screen.findByRole("link", { name: `Open trace of run ${RUN}` }));
    const url = new URL(push.mock.calls[0][0] as string, "http://x");
    expect(url.searchParams.get("level")).toBe("error");
    expect(url.searchParams.get("trace")).toBe(RUN);
  });

  it("searches a selected run over its lifetime, not up to now", async () => {
    vi.useFakeTimers({ toFake: ["Date"] });
    vi.setSystemTime(new Date("2026-11-13T12:00:00Z"));
    currentParams = new URLSearchParams({ run_id: RUN });
    vi.mocked(getRun).mockResolvedValue({
      run_id: RUN, created_at: "2026-11-05T10:00:00Z", updated_at: "2026-11-05T10:00:03Z", display_status: "completed",
      finished_at: "2026-11-05T10:00:02Z",
    } as never);
    vi.mocked(searchLogs).mockResolvedValue(response([log("old run")]));
    render(<LogsPage />);
    expect(await screen.findByText("old run")).toBeInTheDocument();
    expect(searchLogs).toHaveBeenCalledTimes(1);
    expect(vi.mocked(searchLogs).mock.calls[0][0]).toEqual({
      limit: 100, run_id: RUN, from: "2026-11-05T09:59:00.000000Z", to: "2026-11-05T10:00:07.000000Z",
    });
    expect(getSelectOption("Run lifetime")).toBeTruthy();
  });

  it("leaves the window to the API when the run cannot be loaded, and says so", async () => {
    currentParams = new URLSearchParams({ run_id: RUN });
    vi.mocked(searchLogs).mockResolvedValue(response([]));
    render(<LogsPage />);
    await waitFor(() => expect(searchLogs).toHaveBeenCalledTimes(1));
    expect(vi.mocked(searchLogs).mock.calls[0][0]).toEqual({ limit: 100, run_id: RUN });
    expect(await screen.findByRole("option", { name: "Since the run started" })).toBeInTheDocument();
  });

  it("an unknown agent says Agent not found and offers to clear the agent filter", async () => {
    currentParams = new URLSearchParams({ agent_id: AGENT, level: "error" });
    vi.mocked(searchLogs).mockRejectedValue(new ApiError(404, "Agent not found"));
    render(<LogsPage />);
    const error = await screen.findByTestId("logs-error");
    expect(error).toHaveTextContent("Agent not found");
    expect(error).not.toHaveTextContent("Run not found");
    fireEvent.click(screen.getByRole("button", { name: "Clear agent filter" }));
    expect(replace).toHaveBeenLastCalledWith(routes.logs({ level: "error" }), { scroll: false });
  });

  it("still searches the agent's logs when its run suggestions cannot be loaded", async () => {
    currentParams = new URLSearchParams({ agent_id: AGENT });
    vi.mocked(listTraceRuns).mockRejectedValue(new Error("down"));
    vi.mocked(searchLogs).mockResolvedValue({ ...response([log("mine")]), agent_runs: 1, agent_runs_limited: false });
    render(<LogsPage />);
    expect(await screen.findByText("mine")).toBeInTheDocument();
    expect(screen.getByTestId("logs-agent-note")).toHaveTextContent("1 run");
  });

  it("cuts a very long message with an expand control", async () => {
    const long = `start ${"y".repeat(3000)} end`;
    vi.mocked(searchLogs).mockResolvedValue(response([log(long)]));
    render(<LogsPage />);
    const more = await screen.findByRole("button", { name: /Show full message/ });
    expect(more).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByText(/end$/)).toBeNull();
    fireEvent.click(more);
    expect(screen.getByText(/ end$/)).toBeInTheDocument();
  });

  it("drops a pending search debounce when the search text changes from outside (Back/forward)", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    vi.mocked(searchLogs).mockResolvedValue(response([]));
    currentParams = new URLSearchParams({ q: "first" });
    const { rerender } = render(<LogsPage />);
    const box = screen.getByLabelText("Search messages");
    fireEvent.change(box, { target: { value: "first and more" } });
    // Back: the URL's q changes before the debounce fires.
    currentParams = new URLSearchParams({ q: "earlier" });
    rerender(<LogsPage />);
    expect(screen.getByLabelText("Search messages")).toHaveValue("earlier");
    act(() => vi.advanceTimersByTime(SEARCH_DEBOUNCE_MS * 2));
    expect(replace).not.toHaveBeenCalled();
    expect(screen.getByLabelText("Search messages")).toHaveValue("earlier");
  });
});
