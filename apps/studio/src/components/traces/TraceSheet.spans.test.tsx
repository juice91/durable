import { beforeEach, describe, expect, it, vi } from "vitest";
import React, { useState } from "react";
import "@testing-library/jest-dom";
import { act, fireEvent, render, screen, within } from "@testing-library/react";
import { TraceSheet } from "./TraceSheet";
import { TRACE_POLL_MS, traceSignature } from "./useTrace";
import type { TraceTab } from "@/lib/routes/observability";
import { getTrace, type Trace, type TraceSpan } from "@/lib/traces";
import { ApiError } from "@/lib/api";
import { searchLogs } from "@/components/logs/logsQuery";
import { routes } from "@/lib/routes";

// The default logs panel fetches; keep these tests off the network.
vi.mock("@/components/logs/logsQuery", async (importOriginal) => ({
  ...(await importOriginal<typeof import("@/components/logs/logsQuery")>()),
  searchLogs: vi.fn(),
}));

vi.mock("@/lib/traces", async (importOriginal) => ({
  ...(await importOriginal<object>()),
  getTrace: vi.fn(),
}));

vi.mock("@/lib/api", async (importOriginal) => {
  const original = await importOriginal<typeof import("@/lib/api")>();
  return {
    ...original,
    api: { agents: { list: vi.fn(async () => [{ id: "agent-1", name: "Support assistant", draft_spec: {}, latest_version: 2 }]) } },
  };
});

vi.mock("@/lib/workflows", async (importOriginal) => ({
  ...(await importOriginal<object>()),
  listWorkflows: vi.fn(async () => [{ id: "wf-1", name: "Triage", draft_spec: null, latest_version: 3 }]),
}));

const T = (s: number) => new Date(Date.UTC(2026, 10, 13, 10, 0, s)).toISOString();

function span(id: string, parent: string | null, kind: TraceSpan["kind"], name: string, start: number, end: number | null, extra: Partial<TraceSpan> = {}): TraceSpan {
  return {
    id, parent_id: parent, kind, name, start: T(start), end: end === null ? null : T(end), status: "ok",
    attributes: {}, input: null, output: null, truncated: {}, ...extra,
  };
}

const run = {
  run_id: "wfrun-1", kind: "workflow" as const, agent_id: null, version: null, tool_id: null,
  workflow_id: "wf-1", workflow_version: 3, status: "completed", display_status: "completed" as const,
  input: "{}", output: "{}", error: null, heartbeat_at: null, created_at: T(0), updated_at: T(10),
  trace_id: "otel-trace-1", conversation_id: null, turn: null,
};

function fixture(): Trace {
  return {
    run,
    spans: [
      span("run:wfrun-1", null, "run", "workflow wf-1 v3", 0, 10, {
        attributes: { run_id: "wfrun-1", run_kind: "workflow", workflow_id: "wf-1", workflow_version: 3, trace_id: "otel-trace-1", run_status: "completed" },
      }),
      span("wfrun-1:step:classify", "run:wfrun-1", "workflow_step", "classify", 1, 8, {
        attributes: { step_id: "classify", step_type: "agent", child_run_id: "child-1" },
      }),
      span("run:child-1", "wfrun-1:step:classify", "child_run", "agent agent-1 v2 (step classify)", 2, 7, {
        attributes: { run_id: "child-1", run_kind: "agent", agent_id: "agent-1", agent_version: 2, run_status: "completed", input_tokens: 120, output_tokens: 30, cost_usd: "0.0004500000", model_id: "openai:gpt-4.1-mini" },
      }),
      span("child-1:model:0", "run:child-1", "model_request", "openai:gpt-4.1-mini", 2, 5, {
        attributes: {
          model_request: 0, model_id: "openai:gpt-4.1-mini", provider_model_name: "gpt-4.1-mini-2025", cost_usd: "0.0004500000", price_source: "genai-prices",
          usage: { input_tokens: 120, output_tokens: 30, cache_read_tokens: 0, cache_write_tokens: 0, usage_measured: true },
        },
        input: "x".repeat(900),
        output: { text: "done", tool_calls: [] },
        truncated: { input: 20480 },
      }),
      span("child-1:tool:c1", "run:child-1", "tool_call", "lookup", 5, 5, { status: "error", attributes: { tool_name: "lookup", error_code: "tool_failed", error: "boom" } }),
    ],
    totals: {
      runs: 2, model_requests: 1, tool_calls: 1, input_tokens: 120, output_tokens: 30, cache_read_tokens: 0, cache_write_tokens: 0,
      cost_usd: "0.0004500000", unpriced_requests: 0, unrecorded_requests: 0, duration_ms: 10_000, status: "completed",
    },
    truncated: { spans: false, events: false, child_runs: false, payload_fields: 1 },
    limits: { max_spans: 2000, max_events_per_run: 10_000, max_child_runs: 500, field_max_bytes: 8192, payload_budget_bytes: 2097152 },
  };
}


/** The trace Sheet of `runId` on the Spans tab, tabs switchable (it replaced the old trace view). */
function View({ runId }: { runId: string }) {
  const [tab, setTab] = useState<TraceTab>("spans");
  return <TraceSheet runId={runId} open tab={tab} onTabChange={setTab} onOpenChange={() => undefined} />;
}

describe("trace Sheet: spans", () => {
  beforeEach(() => {
    vi.mocked(getTrace).mockReset();
    vi.mocked(searchLogs).mockReset();
    // Never settles: these tests are not about logs, and a late response would update state outside act.
    vi.mocked(searchLogs).mockReturnValue(new Promise(() => undefined));
  });

  it("renders the nested span tree with names, totals, Grafana link and truncation notice", async () => {
    vi.mocked(getTrace).mockResolvedValue(fixture());
    render(<View runId="wfrun-1" />);

    const rows = await screen.findAllByTestId("trace-span-row");
    expect(rows.map((r) => [r.getAttribute("data-span-id"), r.getAttribute("data-depth")])).toEqual([
      ["run:wfrun-1", "0"], ["wfrun-1:step:classify", "1"], ["run:child-1", "2"], ["child-1:model:0", "3"], ["child-1:tool:c1", "3"],
    ]);
    expect(await screen.findByRole("heading", { level: 2, name: "Workflow: Triage v3" })).toBeInTheDocument();
    expect(await screen.findByText("Agent: Support assistant v2")).toBeInTheDocument();
    expect(screen.getByTestId("totals-cost")).toHaveTextContent("$0.00045");
    expect(screen.getByTestId("totals-input-tokens")).toHaveTextContent("120");
    expect(screen.getByTestId("trace-grafana-link").getAttribute("href")).toContain("otel-trace-1");
    expect(screen.getByTestId("trace-truncation")).toHaveTextContent("1 input or output field was cut");
    // Zero-length tool call is drawn as a marker.
    expect(within(rows[4]).getByTestId("trace-span-marker")).toBeInTheDocument();
    expect(rows[4]).toHaveAttribute("data-status", "error");
  });

  it("shows a model request's tokens, cost, collapsed input and server truncation", async () => {
    vi.mocked(getTrace).mockResolvedValue(fixture());
    render(<View runId="wfrun-1" />);
    const rows = await screen.findAllByTestId("trace-span-row");
    fireEvent.click(rows[3]);

    const detail = screen.getByTestId("span-detail");
    expect(within(detail).getByTestId("span-input-tokens")).toHaveTextContent("120");
    expect(within(detail).getByTestId("span-output-tokens")).toHaveTextContent("30");
    expect(within(detail).getByTestId("span-cost")).toHaveTextContent("$0.00045");
    expect(within(detail).getByText("gpt-4.1-mini-2025")).toBeInTheDocument();
    expect(within(detail).getByTestId("payload-input-truncated")).toHaveTextContent("20.0 KB");
    const show = within(detail).getByRole("button", { name: /Show all/ });
    expect(within(detail).getByTestId("payload-input").textContent).not.toContain("x".repeat(900));
    fireEvent.click(show);
    expect(within(detail).getByTestId("payload-input").textContent).toContain("x".repeat(900));
  });

  it("links a child run to its own trace and shows a span error", async () => {
    vi.mocked(getTrace).mockResolvedValue(fixture());
    render(<View runId="wfrun-1" />);
    const rows = await screen.findAllByTestId("trace-span-row");
    fireEvent.click(rows[2]);
    expect(screen.getByTestId("span-link-child-trace")).toHaveAttribute("href", routes.trace("child-1"));
    fireEvent.click(rows[4]);
    expect(screen.getByTestId("span-error")).toHaveTextContent("tool_failed");
  });

  it("supports keyboard navigation and collapsing", async () => {
    vi.mocked(getTrace).mockResolvedValue(fixture());
    render(<View runId="wfrun-1" />);
    const rows = await screen.findAllByTestId("trace-span-row");
    expect(rows[0]).toHaveAttribute("aria-selected", "true");
    fireEvent.keyDown(rows[0], { key: "ArrowDown" });
    expect(screen.getAllByTestId("trace-span-row")[1]).toHaveAttribute("aria-selected", "true");
    expect(document.activeElement).toBe(screen.getAllByTestId("trace-span-row")[1]);
    fireEvent.keyDown(screen.getAllByTestId("trace-span-row")[1], { key: "ArrowLeft" }); // collapse the step
    expect(screen.getAllByTestId("trace-span-row")).toHaveLength(2);
    fireEvent.keyDown(screen.getAllByTestId("trace-span-row")[1], { key: "ArrowLeft" }); // to the parent
    expect(screen.getAllByTestId("trace-span-row")[0]).toHaveAttribute("aria-selected", "true");
    fireEvent.keyDown(screen.getAllByTestId("trace-span-row")[0], { key: "End" });
    expect(screen.getAllByTestId("trace-span-row")[1]).toHaveAttribute("aria-selected", "true");
    fireEvent.keyDown(screen.getAllByTestId("trace-span-row")[1], { key: "ArrowRight" }); // expand
    expect(screen.getAllByTestId("trace-span-row")).toHaveLength(5);
  });

  it("shows 'not recorded' for a run without usage", async () => {
    const t = fixture();
    t.spans = [span("run:old", null, "run", "agent a v1", 0, 2, { attributes: { run_id: "old", run_kind: "agent", agent_id: "agent-1", agent_version: 1 } }),
      span("old:model:0", "run:old", "model_request", "openai:gpt-4.1", 0, 2, { attributes: { model_request: 0, model_id: "openai:gpt-4.1", usage: null, cost_usd: null } })];
    t.totals = { ...t.totals, runs: 1, tool_calls: 0, input_tokens: null, output_tokens: null, cache_read_tokens: null, cache_write_tokens: null, cost_usd: null, unrecorded_requests: 1 };
    t.truncated.payload_fields = 0;
    vi.mocked(getTrace).mockResolvedValue(t);
    render(<View runId="old" />);
    const rows = await screen.findAllByTestId("trace-span-row");
    expect(screen.getByTestId("totals-cost")).toHaveTextContent("not recorded");
    expect(screen.getByTestId("totals-input-tokens")).toHaveTextContent("not recorded");
    expect(screen.queryByTestId("trace-truncation")).toBeNull();
    fireEvent.click(rows[1]);
    expect(screen.getByTestId("span-cost")).toHaveTextContent("not recorded");
  });

  it("the Logs tab gets the selected span's window", async () => {
    vi.mocked(getTrace).mockResolvedValue(fixture());
    render(<View runId="wfrun-1" />);
    const rows = await screen.findAllByTestId("trace-span-row");
    fireEvent.click(rows[3]);
    fireEvent.mouseDown(screen.getByRole("tab", { name: "Logs" }));
    expect(await screen.findByTestId("trace-logs-scope")).toHaveTextContent("Model request #0");
    expect(vi.mocked(searchLogs).mock.calls.at(-1)?.[0]).toMatchObject({ run_id: "wfrun-1", from: "2026-11-13T10:00:02.000000Z" });
  });

  it("shows 'Run not found' for a 404", async () => {
    vi.mocked(getTrace).mockRejectedValue(new ApiError(404, "Run not found"));
    render(<View runId="nope" />);
    expect(await screen.findByRole("alert")).toHaveTextContent("Run not found");
  });

  describe("polling", () => {
    function runningFixture(): Trace {
      const t = fixture();
      t.run = { ...t.run, status: "running", display_status: "running" };
      t.spans = t.spans.slice(0, 2).map((s, i) => (i === 0 ? { ...s, end: null, status: "running" } : s));
      return t;
    }
    const flush = async () => {
      await act(async () => {
        await Promise.resolve();
        await Promise.resolve();
      });
    };

    it("keeps polling after a failed poll and stops once the run finishes", async () => {
      vi.useFakeTimers();
      try {
        const done = fixture();
        vi.mocked(getTrace)
          .mockResolvedValueOnce(runningFixture())
          .mockRejectedValueOnce(new ApiError(500, "boom"))
          .mockResolvedValueOnce(runningFixture())
          .mockResolvedValue(done);
        render(<View runId="wfrun-1" />);
        await flush();
        expect(getTrace).toHaveBeenCalledTimes(1);
        await act(async () => { vi.advanceTimersByTime(TRACE_POLL_MS); });
        await flush();
        expect(getTrace).toHaveBeenCalledTimes(2);
        expect(screen.getByText(/Failed to load the trace; showing the last loaded trace/)).toBeInTheDocument();
        await act(async () => { vi.advanceTimersByTime(TRACE_POLL_MS); });
        await flush();
        expect(getTrace).toHaveBeenCalledTimes(3);
        expect(screen.queryByText(/Failed to load the trace/)).toBeNull();
        await act(async () => { vi.advanceTimersByTime(TRACE_POLL_MS); });
        await flush();
        expect(getTrace).toHaveBeenCalledTimes(4);
        expect(screen.getAllByTestId("trace-span-row")).toHaveLength(5);
        await act(async () => { vi.advanceTimersByTime(TRACE_POLL_MS * 3); });
        await flush();
        expect(getTrace).toHaveBeenCalledTimes(4);
      } finally {
        vi.useRealTimers();
      }
    });

    it("keeps the same rendered rows when a poll brings nothing new", async () => {
      vi.useFakeTimers();
      try {
        vi.mocked(getTrace).mockImplementation(async () => runningFixture());
        render(<View runId="wfrun-1" />);
        await flush();
        const before = screen.getAllByTestId("trace-span-row");
        fireEvent.click(before[1]);
        await act(async () => { vi.advanceTimersByTime(TRACE_POLL_MS); });
        await flush();
        expect(getTrace).toHaveBeenCalledTimes(2);
        const after = screen.getAllByTestId("trace-span-row");
        expect(after[0]).toBe(before[0]);
        expect(after[1]).toHaveAttribute("aria-selected", "true");
      } finally {
        vi.useRealTimers();
      }
    });

    it("signature changes when a span ends or a span is added", () => {
      const a = runningFixture();
      const b = runningFixture();
      expect(traceSignature(a)).toBe(traceSignature(b));
      b.spans[0] = { ...b.spans[0], end: T(9), status: "ok" };
      expect(traceSignature(a)).not.toBe(traceSignature(b));
      // A child run going queued -> running changes only its start and display status.
      const d = runningFixture();
      d.spans[1] = { ...d.spans[1], start: T(3), attributes: { ...d.spans[1].attributes, display_status: "running" } };
      expect(traceSignature(a)).not.toBe(traceSignature(d));
      const e = runningFixture();
      e.spans[1] = { ...e.spans[1], attributes: { ...e.spans[1].attributes, display_status: "queued" } };
      expect(traceSignature(a)).not.toBe(traceSignature(e));
      const c = runningFixture();
      c.spans.push({ ...c.spans[1], id: "extra" });
      expect(traceSignature(a)).not.toBe(traceSignature(c));
    });
  });

  it("drops a response for a run the view has moved away from", async () => {
    let resolveOld!: (v: Trace) => void;
    vi.mocked(getTrace).mockImplementation((id: string) =>
      id === "run-a"
        ? new Promise<Trace>((r) => { resolveOld = r; })
        : Promise.resolve({ ...fixture(), run: { ...run, run_id: "run-b" } }),
    );
    const view = render(<View runId="run-a" />);
    view.rerender(<View runId="run-b" />);
    expect(await screen.findByText("Run run-b")).toBeInTheDocument();
    const stale = fixture();
    stale.run = { ...run, run_id: "run-a" };
    await act(async () => resolveOld(stale));
    expect(screen.getByText("Run run-b")).toBeInTheDocument();
    expect(screen.queryByText("Run run-a")).toBeNull();
  });
});
