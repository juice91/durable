import { beforeEach, describe, expect, it, vi } from "vitest";
import React, { useState } from "react";
import "@testing-library/jest-dom";
import { fireEvent, render, screen } from "@testing-library/react";
import { TraceSheet } from "./TraceSheet";
import type { TraceTab } from "@/lib/routes/observability";
import { getTrace, type Trace, type TraceSpan } from "@/lib/traces";
import { ApiError } from "@/lib/api";
import { searchLogs } from "@/components/logs/logsQuery";

// The default logs panel fetches; keep these tests off the network.
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

const T = (s: number) => new Date(Date.UTC(2026, 10, 13, 10, 0, s)).toISOString();
function span(id: string, parent: string | null, kind: TraceSpan["kind"], name: string, s: number, e: number, extra: Partial<TraceSpan> = {}): TraceSpan {
  return { id, parent_id: parent, kind, name, start: T(s), end: T(e), status: "ok", attributes: {}, input: null, output: null, truncated: {}, ...extra };
}
const run = {
  run_id: "r1", kind: "agent" as const, agent_id: "agent-1", version: 1, tool_id: null, workflow_id: null, workflow_version: null,
  status: "completed", display_status: "completed" as const, input: "x", output: "y", error: null, heartbeat_at: null,
  created_at: T(0), updated_at: T(5), trace_id: null, conversation_id: null, turn: null,
};

function base(): Trace {
  return {
    run,
    spans: [
      span("run:r1", null, "run", "agent a v1", 0, 5, { attributes: { run_id: "r1", run_kind: "agent", agent_id: "agent-1", agent_version: 1 } }),
      span("r1:model:0", "run:r1", "model_request", "test:echo", 0, 2, {
        attributes: { model_request: 0, model_id: "test:echo", cost_usd: null, usage: { input_tokens: 10, output_tokens: 4, cache_read_tokens: 0, cache_write_tokens: 0, usage_measured: true } },
      }),
      span("r1:model:1", "run:r1", "model_request", "test:echo", 2, 4, { attributes: { model_request: 1, model_id: "test:echo", usage: null, cost_usd: null } }),
      span("r1:tool:1", "run:r1", "tool_call", "lookup", 4, 5, { attributes: { tool_name: "lookup" } }),
    ],
    totals: {
      runs: 1, model_requests: 2, tool_calls: 1, input_tokens: 10, output_tokens: 4, cache_read_tokens: 0, cache_write_tokens: 0,
      cost_usd: null, unpriced_requests: 1, unrecorded_requests: 1, duration_ms: 5000, status: "completed",
    },
    truncated: { spans: false, events: false, child_runs: false, payload_fields: 0 },
    limits: { max_spans: 2000, max_events_per_run: 10_000, max_child_runs: 500, field_max_bytes: 8192, payload_budget_bytes: 2097152 },
  };
}


/** The trace Sheet of `runId` on the Spans tab, tabs switchable (it replaced the old trace view). */
function View({ runId }: { runId: string }) {
  const [tab, setTab] = useState<TraceTab>("spans");
  return <TraceSheet runId={runId} open tab={tab} onTabChange={setTab} onOpenChange={() => undefined} />;
}

describe("trace Sheet: spans acceptance", () => {
  beforeEach(() => {
    vi.mocked(getTrace).mockReset();
    vi.mocked(searchLogs).mockReset();
    // Never settles: these tests are not about logs, and a late response would update state outside act.
    vi.mocked(searchLogs).mockReturnValue(new Promise(() => undefined));
  });

  it("an unpriced request says 'no price', an unmeasured one 'not recorded'; totals say 'no price'", async () => {
    vi.mocked(getTrace).mockResolvedValue(base());
    render(<View runId="r1" />);
    const rows = await screen.findAllByTestId("trace-span-row");
    expect(screen.getByTestId("totals-cost")).toHaveTextContent("no price");
    fireEvent.click(rows[1]);
    expect(screen.getByTestId("span-input-tokens")).toHaveTextContent("10");
    expect(screen.getByTestId("span-cost")).toHaveTextContent("no price");
    fireEvent.click(rows[2]);
    expect(screen.getByTestId("span-cost")).toHaveTextContent("not recorded");
  });

  it("each server truncation is announced", async () => {
    const t = base();
    t.truncated = { spans: true, events: true, child_runs: true, payload_fields: 3 };
    vi.mocked(getTrace).mockResolvedValue(t);
    render(<View runId="r1" />);
    const note = await screen.findByTestId("trace-truncation");
    expect(note).toHaveTextContent("Only the first 2,000 spans are shown.");
    expect(note).toHaveTextContent("more than 10,000 events");
    expect(note).toHaveTextContent("Only the first 500 child runs are shown.");
    expect(note).toHaveTextContent("3 input or output fields were cut");
  });

  it("waterfall: Up, Home, End and Enter move and select; the detail follows", async () => {
    vi.mocked(getTrace).mockResolvedValue(base());
    render(<View runId="r1" />);
    const rows = () => screen.getAllByTestId("trace-span-row");
    await screen.findAllByTestId("trace-span-row");
    expect(screen.getByRole("tree", { name: "Trace spans" })).toBeInTheDocument();
    fireEvent.keyDown(rows()[0], { key: "End" });
    expect(rows()[3]).toHaveAttribute("aria-selected", "true");
    expect(document.activeElement).toBe(rows()[3]);
    fireEvent.keyDown(rows()[3], { key: "ArrowUp" });
    expect(rows()[2]).toHaveAttribute("aria-selected", "true");
    fireEvent.keyDown(rows()[2], { key: "Home" });
    expect(rows()[0]).toHaveAttribute("aria-selected", "true");
    fireEvent.keyDown(rows()[0], { key: "ArrowDown" });
    fireEvent.keyDown(rows()[1], { key: "Enter" });
    expect(screen.getByTestId("span-input-tokens")).toHaveTextContent("10");
  });

  it("a 404 says the run was not found", async () => {
    vi.mocked(getTrace).mockRejectedValue(new ApiError(404, "Run not found"));
    render(<View runId="nope" />);
    expect(await screen.findByRole("alert")).toHaveTextContent("Run not found");
  });

  it("another failure shows a generic load error", async () => {
    vi.mocked(getTrace).mockImplementation(async () => {
      throw new ApiError(500, "boom");
    });
    render(<View runId="r2" />);
    expect(await screen.findByRole("alert")).toHaveTextContent("Failed to load the trace");
  });
});
