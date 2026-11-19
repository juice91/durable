import { describe, expect, it } from "vitest";
import { EMPTY_FILTERS, hasActiveFilters, hasRunOnlyFilters, parseTraceFilters, runQueryParams, traceFiltersToParams } from "./traces";
import { externalSpanRows, type ExternalSpan } from "./externalTraces";
import { parseSpanFilters, spanFiltersToParams } from "./spans";

describe("the Source filter", () => {
  it("defaults to all and is not written to the URL then", () => {
    expect(EMPTY_FILTERS.source).toBe("all");
    expect(parseTraceFilters(new URLSearchParams("")).source).toBe("all");
    expect(traceFiltersToParams(EMPTY_FILTERS).toString()).toBe("");
    expect(hasActiveFilters(EMPTY_FILTERS)).toBe(false);
  });

  it("round-trips platform and external, and ignores anything else", () => {
    for (const source of ["platform", "external"] as const) {
      const filters = parseTraceFilters(new URLSearchParams(`source=${source}`));
      expect(filters.source).toBe(source);
      expect(traceFiltersToParams(filters).get("source")).toBe(source);
      expect(hasActiveFilters(filters)).toBe(true);
    }
    expect(parseTraceFilters(new URLSearchParams("source=bogus")).source).toBe("all");
  });

  it("is never part of the GET /runs query, but is of the GET /spans one", () => {
    const filters = parseTraceFilters(new URLSearchParams("source=external&q=x"));
    expect(runQueryParams(filters).toString()).toBe("q=x");
    expect(spanFiltersToParams(parseSpanFilters(new URLSearchParams("source=external"))).get("source")).toBe("external");
  });

  it("run-only filters are the ones an external trace cannot match", () => {
    expect(hasRunOnlyFilters(EMPTY_FILTERS)).toBe(false);
    expect(hasRunOnlyFilters({ ...EMPTY_FILTERS, q: "x", from: "2026-01-01T00:00:00Z" })).toBe(false);
    expect(hasRunOnlyFilters({ ...EMPTY_FILTERS, model: "m" })).toBe(true);
    expect(hasRunOnlyFilters({ ...EMPTY_FILTERS, status: ["failed"] })).toBe(true);
  });
});

describe("externalSpanRows", () => {
  const span = (id: string, parent: string | null): ExternalSpan => ({ span_id: id, parent_id: parent, name: id, start: "2026-11-12T11:00:00Z", end: null, status: "ok", attributes: {} });
  it("nests children under their parent and keeps an orphan as a root", () => {
    const rows = externalSpanRows([span("a", null), span("b", "a"), span("c", "b"), span("o", "missing"), span("d", "a")]);
    expect(rows.map((r) => [r.span.span_id, r.depth])).toEqual([["a", 0], ["b", 1], ["c", 2], ["d", 1], ["o", 0]]);
  });
});
