import { describe, expect, it } from "vitest";
import { parseTraceTab, TRACE_LEGACY_RULES } from "@/lib/routes/observability";
import { legacyTarget, routes } from "@/lib/routes";
import {
  LOG_SHEET_MIN_PX,
  LOG_SHEET_PREFERRED_PX,
  logSheetWidth,
  SPAN_DETAIL_MIN_PX,
  SPAN_GRID_BESIDE_CLASS,
  SPAN_TREE_MIN_PX,
  TRACE_SHEET_CHROME_PX,
  prevNext,
  renderedSheetWidth,
  spanDetailBeside,
  TRACE_LIST_PEEK_PX,
  TRACE_SHEET_MIN_PX,
  traceSheetWidth,
} from "./traceSheetModel";
import { traceRedirectTarget } from "./traceRedirect";

describe("trace Sheet width (the pane fit rule, from the frame geometry)", () => {
  it("covers the main card but the list's first columns, beside the expanded sidebar", () => {
    // 1440 - 240 sidebar - 8 gutter - 280 peek
    expect(traceSheetWidth({ viewport: 1440, sidebarCollapsed: false })).toBe(1440 - 240 - 8 - TRACE_LIST_PEEK_PX);
    // The rail leaves more room.
    expect(traceSheetWidth({ viewport: 1440, sidebarCollapsed: true })).toBe(1440 - 56 - 8 - TRACE_LIST_PEEK_PX);
  });

  it("never goes below the tree + detail minimum, nor past the window", () => {
    expect(traceSheetWidth({ viewport: 1024, sidebarCollapsed: false })).toBe(TRACE_SHEET_MIN_PX);
    expect(traceSheetWidth({ viewport: 800, sidebarCollapsed: false })).toBe(TRACE_SHEET_MIN_PX);
    expect(traceSheetWidth({ viewport: 770, sidebarCollapsed: false })).toBe(Math.min(770, TRACE_SHEET_MIN_PX));
  });

  it("is full width (null) when expanded or below 768 px", () => {
    expect(traceSheetWidth({ viewport: 1440, sidebarCollapsed: false }, true)).toBeNull();
    expect(traceSheetWidth({ viewport: 767, sidebarCollapsed: false })).toBeNull();
    expect(renderedSheetWidth({ viewport: 767, sidebarCollapsed: false }, null)).toBe(767);
  });

  it("the beside threshold and the grid's column minimums are the same numbers", () => {
    expect(TRACE_SHEET_MIN_PX).toBe(SPAN_TREE_MIN_PX + SPAN_DETAIL_MIN_PX + TRACE_SHEET_CHROME_PX);
    expect(SPAN_GRID_BESIDE_CLASS).toBe(`grid-cols-[minmax(${SPAN_TREE_MIN_PX}px,3fr)_minmax(${SPAN_DETAIL_MIN_PX}px,2fr)]`);
    // 1024 and 1280 px: the Sheet is at least the threshold, so beside never squeezes the detail.
    for (const viewport of [1024, 1280]) {
      const width = traceSheetWidth({ viewport, sidebarCollapsed: false })!;
      expect(width).toBeGreaterThanOrEqual(TRACE_SHEET_MIN_PX);
      expect(spanDetailBeside(width)).toBe(true);
      expect(width - TRACE_SHEET_CHROME_PX - SPAN_TREE_MIN_PX).toBeGreaterThanOrEqual(SPAN_DETAIL_MIN_PX);
    }
  });

  it("puts the span detail beside the tree only when both minimums fit", () => {
    expect(spanDetailBeside(TRACE_SHEET_MIN_PX)).toBe(true);
    expect(spanDetailBeside(TRACE_SHEET_MIN_PX - 1)).toBe(false);
    expect(spanDetailBeside(renderedSheetWidth({ viewport: 400, sidebarCollapsed: false }, traceSheetWidth({ viewport: 400, sidebarCollapsed: false })))).toBe(false);
  });

  it("log Sheet: its preferred width, within the window and never under its minimum", () => {
    expect(logSheetWidth({ viewport: 1440, sidebarCollapsed: false })).toBe(LOG_SHEET_PREFERRED_PX);
    // From 768 px the frame always leaves the preferred width; the window caps it.
    expect(logSheetWidth({ viewport: 780, sidebarCollapsed: false })).toBe(LOG_SHEET_PREFERRED_PX);
    expect(logSheetWidth({ viewport: 600, sidebarCollapsed: false })).toBeNull();
    expect(LOG_SHEET_MIN_PX).toBeLessThanOrEqual(LOG_SHEET_PREFERRED_PX);
  });
});

describe("prev / next within the hosting list", () => {
  const ids = ["a", "b", "c"];
  it("gives neighbours and the 1-based position", () => {
    expect(prevNext(ids, "b")).toEqual({ prev: "a", next: "c", position: 2, total: 3 });
    expect(prevNext(ids, "a")).toEqual({ prev: null, next: "b", position: 1, total: 3 });
    expect(prevNext(ids, "c")).toEqual({ prev: "b", next: null, position: 3, total: 3 });
  });
  it("has no position for a run outside the list or no run", () => {
    expect(prevNext(ids, "z")).toEqual({ prev: null, next: null, position: null, total: 3 });
    expect(prevNext(ids, null).position).toBeNull();
    expect(prevNext([], "a").position).toBeNull();
  });
});

describe("trace tabs and the SL.12 legacy rows", () => {
  it("parses the tab parameter, defaulting to spans", () => {
    expect(parseTraceTab("events")).toBe("events");
    expect(parseTraceTab("logs")).toBe("logs");
    expect(parseTraceTab("spans")).toBe("spans");
    expect(parseTraceTab("trace")).toBe("spans");
    expect(parseTraceTab(null)).toBe("spans");
  });

  it.each([
    ["/traces/r1", {}, routes.trace("r1")],
    ["/traces/r1/", { x: "1" }, routes.trace("r1")],
    ["/runs/r1", {}, routes.trace("r1", { tab: "events" })],
    ["/traces/run%3A1%2Fx", {}, "/traces?trace=run%3A1%2Fx"],
    ["/runs/a%20b", {}, "/traces?trace=a+b&tab=events"],
    ["/traces/..", {}, null],
    ["/traces", {}, null],
    ["/runs", {}, null],
  ] as const)("%s %o → %s", (pathname, search, expected) => {
    expect(legacyTarget(pathname, search, TRACE_LEGACY_RULES)).toBe(expected);
  });

  it("the redirect pages' target falls back to the Traces list for a dot-only id", () => {
    expect(traceRedirectTarget(() => routes.legacyRunPath("r1"))).toBe(routes.run("r1"));
    expect(traceRedirectTarget(() => routes.legacyTracePath("."))).toBe(routes.traces());
  });
});
