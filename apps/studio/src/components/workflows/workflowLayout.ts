/**
 * The workflow canvas's layout (SL.14), as pure numbers: the floating left panel (Run and Recent
 * runs, or a run's status), the right-hand column (waiting approvals and the step detail) and the
 * bottom Timeline dock. The canvas is full-bleed in the main card; panels float over it, and the
 * fit rule keeps at least GRAPH_MIN_WIDTH of graph visible beside them. Whatever does not fit
 * becomes a Sheet: the left panel below 768 px (or where even its minimum would squeeze the graph),
 * the step detail where the right column would.
 *
 * The canvas width comes from the frame geometry (`frameGeometry.ts`): the workflow pages open no
 * route side panel, so the canvas is the main card's whole inner width.
 */
import { mainCardWidth } from "@/components/frame/frameGeometry";
import { safeLocalStorage } from "@/lib/safeStorage";

export const WORKFLOW_LAYOUT_STORAGE_KEY = "studio.workflow-layout.v1";

/** Below this window width the left panel is a bottom Sheet and the canvas fills the screen. */
export const NARROW_BREAKPOINT_PX = 768;
/** The narrowest graph the panels may leave visible: one node (200 px) plus a margin. */
export const GRAPH_MIN_WIDTH = 240;
/** The gap between a floating panel and the canvas edge (or another panel). */
export const CANVAS_INSET_PX = 12;

export const PANEL_MIN_WIDTH = 280;
export const PANEL_DEFAULT_WIDTH = 380;
export const PANEL_MAX_WIDTH = 560;
/** The collapsed panel: a rail holding the expand button. */
export const PANEL_RAIL_WIDTH = 40;
export const PANEL_KEY_STEP = 16;

/** The right column (approvals and the step detail). */
export const STEP_PANEL_WIDTH = 320;

export const TIMELINE_BAR_HEIGHT = 36;
export const TIMELINE_DEFAULT_HEIGHT = 240;
export const TIMELINE_MIN_HEIGHT = 120;
export const TIMELINE_MAX_HEIGHT = 480;
export const TIMELINE_KEY_STEP = 16;

/** The main card's horizontal border plus, from 768 px, the 8 px gutter on each side (no sidebar column below 1024 px). */
const NARROW_FRAME_CHROME_PX = 2;
const MEDIUM_FRAME_CHROME_PX = 2 + 16;

export interface CanvasWidthInput {
  viewport: number;
  sidebarCollapsed: boolean;
  /** False when the page renders outside the app frame (a test): no sidebar, no card. */
  inFrame: boolean;
}

/** The canvas's width: the main card's inner width at this window size. */
export function workflowCanvasWidth({ viewport, sidebarCollapsed, inFrame }: CanvasWidthInput): number {
  if (!inFrame) return viewport;
  if (viewport >= 1024) return mainCardWidth({ viewport, sidebarCollapsed }, null);
  return viewport - (viewport >= NARROW_BREAKPOINT_PX ? MEDIUM_FRAME_CHROME_PX : NARROW_FRAME_CHROME_PX);
}

/** The widest the left panel may be on a canvas `canvasWidth` px wide (never below the minimum). */
export function panelMaxWidth(canvasWidth: number): number {
  return Math.max(PANEL_MIN_WIDTH, Math.min(PANEL_MAX_WIDTH, Math.floor(canvasWidth - GRAPH_MIN_WIDTH - 2 * CANVAS_INSET_PX)));
}

/** `width` within [minimum, panelMaxWidth]; a non-finite width becomes the default (then clamped). */
export function clampPanelWidth(width: number, canvasWidth: number): number {
  const base = Number.isFinite(width) ? width : PANEL_DEFAULT_WIDTH;
  return Math.round(Math.min(Math.max(base, PANEL_MIN_WIDTH), panelMaxWidth(canvasWidth)));
}

export function clampTimelineHeight(height: number): number {
  const base = Number.isFinite(height) ? height : TIMELINE_DEFAULT_HEIGHT;
  return Math.round(Math.min(Math.max(base, TIMELINE_MIN_HEIGHT), TIMELINE_MAX_HEIGHT));
}

export interface LayoutInput {
  viewport: number;
  canvasWidth: number;
  /** The saved panel width (clamped here). */
  panelWidth: number;
  panelCollapsed: boolean;
}

export interface WorkflowLayout {
  /** The left panel floats over the canvas, or is a bottom Sheet. */
  panel: "floating" | "sheet";
  /** The panel's width when floating and expanded. */
  panelWidth: number;
  /** Canvas pixels the left panel covers (0 as a Sheet; the rail when collapsed). */
  leftOccupied: number;
  /** The step detail floats in a right column, or opens as a Sheet. */
  stepDetail: "floating" | "sheet";
}

/** Where each panel goes for this window and canvas (the fit rule). */
export function workflowLayout({ viewport, canvasWidth, panelWidth, panelCollapsed }: LayoutInput): WorkflowLayout {
  const panelFits = viewport >= NARROW_BREAKPOINT_PX && canvasWidth - 2 * CANVAS_INSET_PX - GRAPH_MIN_WIDTH >= PANEL_MIN_WIDTH;
  const panel = panelFits ? "floating" : "sheet";
  const width = clampPanelWidth(panelWidth, canvasWidth);
  const leftOccupied = panel === "sheet" ? 0 : panelCollapsed ? PANEL_RAIL_WIDTH : width;
  const leftInsets = leftOccupied > 0 ? 2 * CANVAS_INSET_PX : CANVAS_INSET_PX;
  const rightRoom = canvasWidth - leftOccupied - leftInsets - CANVAS_INSET_PX - GRAPH_MIN_WIDTH;
  const stepDetail = panel === "floating" && rightRoom >= STEP_PANEL_WIDTH ? "floating" : "sheet";
  return { panel, panelWidth: width, leftOccupied, stepDetail };
}

export interface WorkflowLayoutPrefs {
  panelWidth: number;
  panelCollapsed: boolean;
  timelineOpen: boolean;
  timelineHeight: number;
}

export const DEFAULT_LAYOUT_PREFS: WorkflowLayoutPrefs = {
  panelWidth: PANEL_DEFAULT_WIDTH,
  panelCollapsed: false,
  timelineOpen: false,
  timelineHeight: TIMELINE_DEFAULT_HEIGHT,
};

/** The saved layout, field by field: a missing or malformed field keeps its default. */
export function readLayoutPrefs(): WorkflowLayoutPrefs {
  try {
    const raw = safeLocalStorage()?.getItem(WORKFLOW_LAYOUT_STORAGE_KEY);
    if (!raw) return DEFAULT_LAYOUT_PREFS;
    const parsed: unknown = JSON.parse(raw);
    if (!parsed || typeof parsed !== "object") return DEFAULT_LAYOUT_PREFS;
    const value = parsed as Record<string, unknown>;
    const positive = (n: unknown): n is number => typeof n === "number" && Number.isFinite(n) && n > 0;
    return {
      panelWidth: positive(value.panelWidth) ? value.panelWidth : DEFAULT_LAYOUT_PREFS.panelWidth,
      panelCollapsed: typeof value.panelCollapsed === "boolean" ? value.panelCollapsed : DEFAULT_LAYOUT_PREFS.panelCollapsed,
      timelineOpen: typeof value.timelineOpen === "boolean" ? value.timelineOpen : DEFAULT_LAYOUT_PREFS.timelineOpen,
      timelineHeight: positive(value.timelineHeight) ? clampTimelineHeight(value.timelineHeight) : DEFAULT_LAYOUT_PREFS.timelineHeight,
    };
  } catch {
    return DEFAULT_LAYOUT_PREFS;
  }
}

export function writeLayoutPrefs(prefs: WorkflowLayoutPrefs): void {
  try {
    safeLocalStorage()?.setItem(
      WORKFLOW_LAYOUT_STORAGE_KEY,
      JSON.stringify({ ...prefs, panelWidth: Math.round(prefs.panelWidth), timelineHeight: Math.round(prefs.timelineHeight) }),
    );
  } catch {
    // Not saved: the layout reopens with its defaults.
  }
}
