import { describe, expect, it } from "vitest";
import { filterSpanRows, flattenSpans, type TraceSpan } from "./traces";

function span(id: string, parent: string | null, kind: TraceSpan["kind"], name: string): TraceSpan {
  return { id, parent_id: parent, kind, name, start: "2026-11-16T10:00:00Z", end: "2026-11-16T10:00:01Z", status: "ok", attributes: {}, input: null, output: null, truncated: {} };
}

const SPANS = [
  span("root", null, "run", "agent a"),
  span("m1", "root", "model_request", "gpt"),
  span("t1", "root", "tool_call", "lookup"),
  span("p1", "t1", "processor", "redact"),
  span("t2", "root", "tool_call", "send"),
];
const ids = (rows: ReturnType<typeof flattenSpans>) => rows.map((r) => r.span.id);

describe("filterSpanRows", () => {
  const rows = flattenSpans(SPANS);

  it("an empty query keeps every row", () => {
    expect(ids(filterSpanRows(rows, "  "))).toEqual(["root", "m1", "t1", "p1", "t2"]);
  });

  it("matches the span label case-insensitively and keeps the ancestors of a match", () => {
    expect(ids(filterSpanRows(rows, "REDACT"))).toEqual(["root", "t1", "p1"]);
    expect(ids(filterSpanRows(rows, "tool call: send"))).toEqual(["root", "t2"]);
  });

  it("matches the kind", () => {
    expect(ids(filterSpanRows(rows, "model_request"))).toEqual(["root", "m1"]);
  });

  it("returns nothing when no row matches", () => {
    expect(filterSpanRows(rows, "zzz")).toEqual([]);
  });
});
