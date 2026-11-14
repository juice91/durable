import { afterEach, describe, expect, it } from "vitest";
import { mainCardWidth } from "@/components/frame/frameGeometry";
import {
  CANVAS_INSET_PX,
  clampPanelWidth,
  clampTimelineHeight,
  DEFAULT_LAYOUT_PREFS,
  GRAPH_MIN_WIDTH,
  PANEL_DEFAULT_WIDTH,
  PANEL_MAX_WIDTH,
  PANEL_MIN_WIDTH,
  PANEL_RAIL_WIDTH,
  panelMaxWidth,
  readLayoutPrefs,
  TIMELINE_MAX_HEIGHT,
  TIMELINE_MIN_HEIGHT,
  WORKFLOW_LAYOUT_STORAGE_KEY,
  workflowCanvasWidth,
  workflowLayout,
  writeLayoutPrefs,
} from "./workflowLayout";

afterEach(() => localStorage.clear());

function layoutAt(viewport: number, { sidebarCollapsed = false, panelWidth = PANEL_DEFAULT_WIDTH, panelCollapsed = false } = {}) {
  const canvasWidth = workflowCanvasWidth({ viewport, sidebarCollapsed, inFrame: true });
  return { canvasWidth, ...workflowLayout({ viewport, canvasWidth, panelWidth, panelCollapsed }) };
}

describe("workflowCanvasWidth", () => {
  it("is the main card's inner width from the frame geometry at 1024 px and up", () => {
    expect(workflowCanvasWidth({ viewport: 1440, sidebarCollapsed: false, inFrame: true })).toBe(mainCardWidth({ viewport: 1440, sidebarCollapsed: false }, null));
    expect(workflowCanvasWidth({ viewport: 1440, sidebarCollapsed: true, inFrame: true })).toBe(mainCardWidth({ viewport: 1440, sidebarCollapsed: true }, null));
  });

  it("has no sidebar column below 1024 px, and no gutter below 768 px", () => {
    expect(workflowCanvasWidth({ viewport: 900, sidebarCollapsed: false, inFrame: true })).toBe(900 - 18);
    expect(workflowCanvasWidth({ viewport: 375, sidebarCollapsed: false, inFrame: true })).toBe(373);
  });

  it("is the window outside the frame", () => {
    expect(workflowCanvasWidth({ viewport: 1024, sidebarCollapsed: false, inFrame: false })).toBe(1024);
  });
});

describe("the fit rule", () => {
  it("never lets the panel squeeze the graph below its minimum", () => {
    for (const viewport of [768, 900, 1024, 1280, 1440, 1920]) {
      for (const sidebarCollapsed of [false, true]) {
        const { canvasWidth, panel, panelWidth } = layoutAt(viewport, { sidebarCollapsed, panelWidth: 10_000 });
        expect(panel).toBe("floating");
        expect(panelWidth).toBeLessThanOrEqual(PANEL_MAX_WIDTH);
        expect(canvasWidth - panelWidth - 2 * CANVAS_INSET_PX).toBeGreaterThanOrEqual(GRAPH_MIN_WIDTH);
      }
    }
  });

  it("turns the panel into a bottom Sheet below 768 px (375 px included)", () => {
    for (const viewport of [320, 375, 400, 767]) {
      const layout = layoutAt(viewport);
      expect(layout.panel).toBe("sheet");
      expect(layout.leftOccupied).toBe(0);
      expect(layout.stepDetail).toBe("sheet");
    }
    expect(layoutAt(768).panel).toBe("floating");
  });

  it("floats the step detail only where it still leaves the minimum graph", () => {
    // 1024 px with the sidebar expanded: 774 px of canvas, no room for a 320 px column.
    expect(layoutAt(1024).stepDetail).toBe("sheet");
    expect(layoutAt(1280).stepDetail).toBe("floating");
    // Collapsing the panel to its rail frees the room again.
    expect(layoutAt(1024, { panelCollapsed: true }).stepDetail).toBe("floating");
    expect(layoutAt(1024, { panelCollapsed: true }).leftOccupied).toBe(PANEL_RAIL_WIDTH);
  });

  it("clamps the panel width into [minimum, maximum], defaulting a non-finite one", () => {
    expect(clampPanelWidth(100, 1200)).toBe(PANEL_MIN_WIDTH);
    expect(clampPanelWidth(10_000, 2000)).toBe(PANEL_MAX_WIDTH);
    expect(clampPanelWidth(Number.NaN, 2000)).toBe(PANEL_DEFAULT_WIDTH);
    expect(panelMaxWidth(500)).toBe(PANEL_MIN_WIDTH);
    expect(panelMaxWidth(800)).toBe(800 - GRAPH_MIN_WIDTH - 2 * CANVAS_INSET_PX);
  });

  it("clamps the timeline height", () => {
    expect(clampTimelineHeight(10)).toBe(TIMELINE_MIN_HEIGHT);
    expect(clampTimelineHeight(10_000)).toBe(TIMELINE_MAX_HEIGHT);
  });
});

describe("layout preferences", () => {
  it("round-trips through localStorage", () => {
    writeLayoutPrefs({ panelWidth: 420.4, panelCollapsed: true, timelineOpen: true, timelineHeight: 300 });
    expect(JSON.parse(localStorage.getItem(WORKFLOW_LAYOUT_STORAGE_KEY)!)).toEqual({ panelWidth: 420, panelCollapsed: true, timelineOpen: true, timelineHeight: 300 });
    expect(readLayoutPrefs()).toEqual({ panelWidth: 420, panelCollapsed: true, timelineOpen: true, timelineHeight: 300 });
  });

  it("falls back field by field on malformed data", () => {
    expect(readLayoutPrefs()).toEqual(DEFAULT_LAYOUT_PREFS);
    localStorage.setItem(WORKFLOW_LAYOUT_STORAGE_KEY, "{not json");
    expect(readLayoutPrefs()).toEqual(DEFAULT_LAYOUT_PREFS);
    localStorage.setItem(WORKFLOW_LAYOUT_STORAGE_KEY, JSON.stringify({ panelWidth: -5, panelCollapsed: "yes", timelineOpen: true, timelineHeight: 99_999 }));
    expect(readLayoutPrefs()).toEqual({ ...DEFAULT_LAYOUT_PREFS, timelineOpen: true, timelineHeight: TIMELINE_MAX_HEIGHT });
  });
});
