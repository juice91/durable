import { beforeEach, describe, expect, it, onTestFinished, vi } from "vitest";
import React from "react";
import "@testing-library/jest-dom";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { ApiError } from "@/lib/api";
import { getTrace, type Trace, type TraceSpan } from "@/lib/traces";
import { searchLogs, type LogItem } from "@/components/logs/logsQuery";
import { TraceLogsPanel } from "./TraceLogsPanel";
import { TraceView } from "./TraceView";
import { routes } from "@/lib/routes";

vi.mock("@/components/logs/logsQuery", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/components/logs/logsQuery")>()),
  searchLogs: vi.fn(),
}));
vi.mock("@/lib/traces", async (importOriginal) => ({ ...(await importOriginal<object>()), getTrace: vi.fn() }));
vi.mock("@/lib/api", async (importOriginal) => {
  const original = await importOriginal<typeof import("@/lib/api")>();
  return { ...original, api: { agents: { list: vi.fn(async () => []) } } };
});
vi.mock("@/lib/workflows", async (importOriginal) => ({ ...(await importOriginal<object>()), listWorkflows: vi.fn(async () => []) }));

const RUN = "11111111-2222-3333-4444-555555555555";
const T = (s: number) => new Date(Date.UTC(2026, 10, 13, 10, 0, s)).toISOString();

function span(id: string, parent: string | null, kind: TraceSpan["kind"], name: string, start: number, end: number | null, attributes: TraceSpan["attributes"] = {}): TraceSpan {
  return { id, parent_id: parent, kind, name, start: T(start), end: end === null ? null : T(end), status: "ok", attributes, input: null, output: null, truncated: {} };
}

function log(message: string, s: number): LogItem {
  return { ts: T(s), level: "info", message, service: "eap-worker", trace_id: null, span_id: null, run_id: RUN };
}

const response = (items: LogItem[], limit = 500) => ({ items, from: T(0), to: T(60), limit });

describe("TraceLogsPanel", () => {
  beforeEach(() => {
    vi.mocked(searchLogs).mockReset();
  });

  const model = span("m0", "run", "model_request", "test:model", 2, 5, { model_id: "test:model", model_request: 0 });

  it("queries the run's logs in the selected span's window and lists them oldest first", async () => {
    vi.mocked(searchLogs).mockResolvedValue(response([log("second", 4), log("first", 3)]));
    render(<TraceLogsPanel runId={RUN} selectedSpan={model} window={{ start: model.start, end: model.end }} />);
    const rows = await screen.findAllByTestId("log-row");
    expect(rows.map((r) => within(r).getByText(/first|second/).textContent)).toEqual(["first", "second"]);
    expect(vi.mocked(searchLogs).mock.calls[0][0]).toEqual({
      run_id: RUN, limit: 500, from: "2026-11-13T10:00:02.000000Z", to: "2026-11-13T10:00:05.001000Z",
    });
    expect(screen.getByTestId("trace-logs-scope")).toHaveTextContent("Model request #0: test:model");
    expect(screen.getByRole("link", { name: "Search in Logs" })).toHaveAttribute("href", `${routes.logs()}?run_id=${RUN}`);
  });

  it("shows all run logs on request, and back", async () => {
    vi.mocked(searchLogs).mockResolvedValue(response([log("a", 3)]));
    render(<TraceLogsPanel runId={RUN} selectedSpan={model} window={{ start: model.start, end: model.end }} />);
    await screen.findByText("a");
    const toggle = screen.getByRole("button", { name: "Show all run logs" });
    expect(toggle).toHaveAttribute("aria-pressed", "false");
    fireEvent.click(toggle);
    await waitFor(() => expect(searchLogs).toHaveBeenCalledTimes(2));
    expect(vi.mocked(searchLogs).mock.calls[1][0]).toEqual({ run_id: RUN, limit: 500 });
    expect(screen.getByTestId("trace-logs-scope")).toHaveTextContent("All records logged by this run");
    fireEvent.click(screen.getByRole("button", { name: "Only the selected span" }));
    await waitFor(() => expect(searchLogs).toHaveBeenCalledTimes(3));
    expect(vi.mocked(searchLogs).mock.calls[2][0]).toMatchObject({ from: "2026-11-13T10:00:02.000000Z" });
  });

  it("shows an old run's logs over its lifetime, not the API's window up to now", async () => {
    vi.useFakeTimers({ toFake: ["Date"] });
    vi.setSystemTime(new Date("2026-11-13T12:00:00Z"));
    onTestFinished(() => {
      vi.useRealTimers();
    });
    const run = { created_at: "2026-11-05T10:00:00Z", finished_at: "2026-11-05T10:00:02Z", updated_at: "2026-11-05T10:00:03Z", display_status: "completed" };
    vi.mocked(searchLogs).mockResolvedValue(response([log("eight days ago", 1)]));
    render(<TraceLogsPanel runId={RUN} run={run} selectedSpan={model} window={{ start: model.start, end: model.end }} />);
    await screen.findByText("eight days ago");
    fireEvent.click(screen.getByRole("button", { name: "Show all run logs" }));
    await waitFor(() => expect(searchLogs).toHaveBeenCalledTimes(2));
    expect(vi.mocked(searchLogs).mock.calls[1][0]).toEqual({
      run_id: RUN, limit: 500, from: "2026-11-05T09:59:00.000000Z", to: "2026-11-05T10:00:07.000000Z",
    });
  });

  it("offers all run logs from the empty state", async () => {
    vi.mocked(searchLogs).mockResolvedValueOnce(response([])).mockResolvedValueOnce(response([log("elsewhere", 9)]));
    render(<TraceLogsPanel runId={RUN} selectedSpan={model} window={{ start: model.start, end: model.end }} />);
    const empty = await screen.findByTestId("trace-logs-empty");
    expect(empty).toHaveTextContent("No logs during this span.");
    fireEvent.click(within(empty).getByRole("button", { name: "Show all run logs" }));
    expect(await screen.findByText("elsewhere")).toBeInTheDocument();
  });

  it("shows the Loki-unavailable state", async () => {
    vi.mocked(searchLogs).mockRejectedValue(new ApiError(503, "Log search is not configured"));
    render(<TraceLogsPanel runId={RUN} selectedSpan={null} window={{ start: null, end: null }} />);
    expect(await screen.findByTestId("logs-unavailable")).toHaveTextContent("Log search is not configured");
    expect(screen.queryByRole("button", { name: "Show all run logs" })).toBeNull();
  });

  it("is the trace view's default panel and follows span selection", async () => {
    const trace: Trace = {
      run: {
        run_id: RUN, kind: "agent", agent_id: "agent-1", version: 1, tool_id: null, workflow_id: null, workflow_version: null,
        status: "completed", display_status: "completed", input: "hi", output: "ok", error: null, heartbeat_at: null,
        created_at: T(0), updated_at: T(10), trace_id: null, conversation_id: null, turn: null,
      } as Trace["run"],
      spans: [span("run", null, "run", "agent agent-1 v1", 0, 10, { run_kind: "agent", agent_id: "agent-1", agent_version: 1 }), model],
      totals: {
        runs: 1, model_requests: 1, tool_calls: 0, input_tokens: null, output_tokens: null, cache_read_tokens: null, cache_write_tokens: null,
        cost_usd: null, unpriced_requests: 0, unrecorded_requests: 0, duration_ms: 10_000, status: "completed",
      },
      truncated: { spans: false, events: false, child_runs: false, payload_fields: 0 },
      limits: { max_spans: 2000, max_events_per_run: 10_000, max_child_runs: 500, field_max_bytes: 8192, payload_budget_bytes: 2097152 },
    };
    vi.mocked(getTrace).mockResolvedValue(trace);
    vi.mocked(searchLogs).mockResolvedValue(response([log("calling the model", 3)]));
    render(<TraceView runId={RUN} />);
    const rows = await screen.findAllByTestId("trace-span-row");
    await waitFor(() => expect(searchLogs).toHaveBeenCalled());
    expect(vi.mocked(searchLogs).mock.calls.at(-1)?.[0]).toMatchObject({ from: "2026-11-13T10:00:00.000000Z", to: "2026-11-13T10:00:10.001000Z" });
    fireEvent.click(rows[1]);
    await waitFor(() => expect(vi.mocked(searchLogs).mock.calls.at(-1)?.[0]).toMatchObject({ from: "2026-11-13T10:00:02.000000Z", to: "2026-11-13T10:00:05.001000Z" }));
    const panel = screen.getByTestId("trace-logs-panel");
    expect(await within(panel).findByText("calling the model")).toBeInTheDocument();
  });

  it("can be turned off", async () => {
    vi.mocked(getTrace).mockResolvedValue({
      run: { run_id: RUN, display_status: "completed", created_at: T(0), updated_at: T(1), trace_id: null } as Trace["run"],
      spans: [span("run", null, "run", "r", 0, 1)],
      totals: { runs: 1, model_requests: 0, tool_calls: 0, input_tokens: null, output_tokens: null, cache_read_tokens: null, cache_write_tokens: null, cost_usd: null, unpriced_requests: 0, unrecorded_requests: 0, duration_ms: 1000, status: "completed" },
      truncated: { spans: false, events: false, child_runs: false, payload_fields: 0 },
      limits: { max_spans: 1, max_events_per_run: 1, max_child_runs: 1, field_max_bytes: 1, payload_budget_bytes: 1 },
    });
    render(<TraceView runId={RUN} renderLogsPanel={null} />);
    await screen.findAllByTestId("trace-span-row");
    expect(screen.queryByTestId("trace-logs-panel")).toBeNull();
    expect(searchLogs).not.toHaveBeenCalled();
  });
});
