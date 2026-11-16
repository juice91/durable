import { describe, expect, it } from "vitest";
import {
  flattenSpans,
  formatDuration,
  parseTraceFilters,
  spanGeometry,
  spanLabel,
  traceFiltersToParams,
  traceWindow,
  type TraceSpan,
} from "./traces";

function span(id: string, parent: string | null, start: string | null, end: string | null, extra: Partial<TraceSpan> = {}): TraceSpan {
  return {
    id, parent_id: parent, kind: "tool_call", name: id, start, end, status: "ok",
    attributes: {}, input: null, output: null, truncated: {}, ...extra,
  };
}

const T = (s: number) => new Date(Date.UTC(2026, 10, 13, 10, 0, s)).toISOString();

describe("flattenSpans", () => {
  const spans = [
    span("run", null, T(0), T(10), { kind: "run" }),
    span("a", "run", T(1), T(3)),
    span("b", "run", T(2), T(6)),
    span("a1", "a", T(1), T(2)),
    span("orphan", "missing", T(4), T(5)),
  ];

  it("orders depth-first with depths and child counts; orphans become roots", () => {
    const rows = flattenSpans(spans);
    expect(rows.map((r) => [r.span.id, r.depth, r.children])).toEqual([
      ["run", 0, 2], ["a", 1, 1], ["a1", 2, 0], ["b", 1, 0], ["orphan", 0, 0],
    ]);
  });

  it("hides the descendants of a collapsed span", () => {
    expect(flattenSpans(spans, new Set(["a"])).map((r) => r.span.id)).toEqual(["run", "a", "b", "orphan"]);
    expect(flattenSpans(spans, new Set(["run"])).map((r) => r.span.id)).toEqual(["run", "orphan"]);
  });
});

describe("waterfall geometry", () => {
  it("places nested and parallel spans on the trace window", () => {
    const spans = [span("run", null, T(0), T(10)), span("p1", "run", T(2), T(6)), span("p2", "run", T(2), T(4))];
    const w = traceWindow(spans)!;
    expect(w.durationMs).toBe(10_000);
    expect(spanGeometry(spans[0], w)).toMatchObject({ offsetPct: 0, widthPct: 100, durationMs: 10_000 });
    expect(spanGeometry(spans[1], w)).toMatchObject({ offsetPct: 20, widthPct: 40, durationMs: 4000 });
    expect(spanGeometry(spans[2], w)).toMatchObject({ offsetPct: 20, widthPct: 20 });
  });

  it("marks zero-length spans and runs open spans to now", () => {
    const now = Date.parse(T(20));
    const spans = [span("run", null, T(0), null), span("z", "run", T(5), T(5))];
    const w = traceWindow(spans, now)!;
    expect(w.durationMs).toBe(20_000);
    expect(spanGeometry(spans[1], w, now)).toMatchObject({ zeroLength: true, widthPct: 0, offsetPct: 25, durationMs: 0 });
    expect(spanGeometry(spans[0], w, now)).toMatchObject({ open: true, widthPct: 100, durationMs: null });
  });

  it("handles a trace of only zero-length spans and missing starts", () => {
    const spans = [span("a", null, T(1), T(1)), span("b", null, null, null)];
    const w = traceWindow(spans)!;
    expect(w.durationMs).toBe(0);
    expect(spanGeometry(spans[0], w)).toMatchObject({ offsetPct: 0, widthPct: 0, zeroLength: true });
    expect(spanGeometry(spans[1], w)).toMatchObject({ widthPct: 0, durationMs: null });
    expect(traceWindow([span("x", null, null, null)])).toBeNull();
  });
});

describe("spanLabel", () => {
  const names = { agentNames: { "agent-1": "Support" }, workflowNames: { "wf-1": "Triage" } };
  it("labels run spans with agent and workflow names, falling back to short ids", () => {
    expect(spanLabel(span("r", null, null, null, { kind: "run", attributes: { run_kind: "agent", agent_id: "agent-1", agent_version: 2 } }), names)).toBe("Agent: Support v2");
    expect(spanLabel(span("r", null, null, null, { kind: "run", attributes: { run_kind: "workflow", workflow_id: "wf-1", workflow_version: 3 } }), names)).toBe("Workflow: Triage v3");
    expect(spanLabel(span("r", null, null, null, { kind: "child_run", attributes: { run_kind: "agent", agent_id: "0123456789ab", agent_version: 1, turn: 2 } }))).toBe("Agent: 01234567... v1 (turn 2)");
    expect(spanLabel(span("r", null, null, null, { kind: "run", attributes: { run_kind: "tool", tool_id: "clock.now" } }))).toBe("Tool: clock.now");
  });
  it("labels model requests, tools, steps and processors", () => {
    expect(spanLabel(span("m", null, null, null, { kind: "model_request", name: "test:echo", attributes: { model_request: 0, model_id: "test:echo" } }))).toBe("Model request #0: test:echo");
    expect(spanLabel(span("t", null, null, null, { name: "lookup" }))).toBe("Tool call: lookup");
    expect(spanLabel(span("s", null, null, null, { kind: "workflow_step", name: "classify", attributes: { step_type: "agent" } }))).toBe("Step: classify (agent)");
  });
});

describe("formatting", () => {
  it("formats durations", () => {
    expect(formatDuration(null)).toBe("-");
    expect(formatDuration(0)).toBe("0 ms");
    expect(formatDuration(1500)).toBe("1.50 s");
    expect(formatDuration(125_000)).toBe("2 min 5 s");
    expect(formatDuration(120_000)).toBe("2 min");
    expect(formatDuration(3_900_000)).toBe("1 h 5 min");
    expect(formatDuration(-1)).toBe("-");
  });
});

describe("URL filters", () => {
  it("round-trips the drill-down contract", () => {
    const query = "agent_id=a1&model=openai%3Agpt-4.1&status=failed,needs_attention&from=2026-11-01T00%3A00%3A00&to=2026-11-02T00%3A00%3A00&kind=agent&provider=openai";
    const filters = parseTraceFilters(new URLSearchParams(query));
    expect(filters).toEqual({
      status: ["failed", "needs_attention"], from: "2026-11-01T00:00:00", to: "2026-11-02T00:00:00",
      model: "openai:gpt-4.1", provider: "openai", agent_id: "a1", kind: "agent",
      tags: [], metadata: [], q: null,
    });
    expect(parseTraceFilters(traceFiltersToParams(filters))).toEqual(filters);
  });
  it("accepts repeated status params, drops blanks and unknown kinds", () => {
    const f = parseTraceFilters(new URLSearchParams("status=failed&status=running&kind=nope&model="));
    expect(f.status).toEqual(["failed", "running"]);
    expect(f.kind).toBeNull();
    expect(f.model).toBeNull();
    expect(traceFiltersToParams(f).toString()).toBe("status=failed%2Crunning");
    expect(traceFiltersToParams({ ...f, status: ["recovering"] }).toString()).toBe("status=recovering");
  });
});

describe("kb_ingest runs (SL.15 review M16)", () => {
  it("the Traces kind filter accepts kb_ingest and round-trips it", () => {
    const filters = parseTraceFilters(new URLSearchParams("kind=kb_ingest"));
    expect(filters.kind).toBe("kb_ingest");
    expect(traceFiltersToParams(filters).get("kind")).toBe("kb_ingest");
    expect(parseTraceFilters(new URLSearchParams("kind=bogus")).kind).toBeNull();
  });

  it("a kb_ingest run span is labelled Knowledge ingest", () => {
    const span = { id: "root", parent_id: null, kind: "run", name: "run", start: null, end: null, status: "ok", attributes: { run_kind: "kb_ingest" }, input: null, output: null, truncated: {} } as unknown as Parameters<typeof spanLabel>[0];
    expect(spanLabel(span)).toBe("Knowledge ingest");
  });
});
