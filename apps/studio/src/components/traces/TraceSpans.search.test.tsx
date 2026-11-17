import React from "react";
import "@testing-library/jest-dom";
import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { TraceSpans } from "./TraceSpans";
import type { Trace, TraceSpan } from "@/lib/traces";

function span(id: string, parent: string | null, kind: TraceSpan["kind"], name: string): TraceSpan {
  return { id, parent_id: parent, kind, name, start: "2026-11-16T10:00:00Z", end: "2026-11-16T10:00:01Z", status: "ok", attributes: {}, input: null, output: null, truncated: {} };
}
const trace = {
  run: { run_id: "r1", kind: "agent", status: "completed", display_status: "completed" },
  spans: [span("root", null, "run", "agent a"), span("t1", "root", "tool_call", "lookup"), span("p1", "t1", "processor", "redact"), span("t2", "root", "tool_call", "send")],
  totals: { runs: 1, model_requests: 0, tool_calls: 2, input_tokens: null, output_tokens: null, cache_read_tokens: null, cache_write_tokens: null, cost_usd: null, unpriced_requests: 0, unrecorded_requests: 0, duration_ms: 1000, status: "completed" },
  truncated: { spans: false, events: false, child_runs: false, payload_fields: 0 },
  limits: { max_spans: 2000, max_events_per_run: 10000, max_child_runs: 500, field_max_bytes: 8192, payload_budget_bytes: 2097152 },
} as unknown as Trace;

const rowIds = () => screen.getAllByTestId("trace-span-row").map((r) => r.getAttribute("data-span-id"));

describe("span search", () => {
  it("filters the rows by name and kind, keeping ancestors, and clears back to all", () => {
    render(<TraceSpans trace={trace} names={{}} selectedId="root" onSelect={() => undefined} />);
    expect(rowIds()).toEqual(["root", "t1", "p1", "t2"]);
    const box = screen.getByRole("searchbox", { name: "Search spans" });
    fireEvent.change(box, { target: { value: "redact" } });
    expect(rowIds()).toEqual(["root", "t1", "p1"]);
    fireEvent.change(box, { target: { value: "tool_call" } });
    expect(rowIds()).toEqual(["root", "t1", "t2"]);
    fireEvent.change(box, { target: { value: "nothing here" } });
    expect(screen.getByText("No spans match this search.")).toBeInTheDocument();
    fireEvent.change(box, { target: { value: "" } });
    expect(rowIds()).toEqual(["root", "t1", "p1", "t2"]);
  });
});
