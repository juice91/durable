/**
 * The trace and log Sheets' pure decisions (SL.12), unit-tested with injected widths (jsdom does no
 * layout): prev / next within the hosting list, and the Sheets' widths from the frame geometry (the
 * pane fit rule shared with the agent page: `frameGeometry`), instead of fixed widths.
 */
import { FRAME_GUTTER_PX, sidebarWidth, type FrameSize } from "@/components/frame/frameGeometry";

/** Below this window width every Sheet is full width (the kit's `md` breakpoint). */
export const SHEET_FULL_WIDTH_BELOW_PX = 768;
/** The span tree (and the span list's labels) need at least this beside the detail column. */
export const SPAN_TREE_MIN_PX = 360;
/** The span detail column's minimum when it sits beside the tree. */
export const SPAN_DETAIL_MIN_PX = 320;
/** The Sheet body's horizontal padding (`p-4`, both sides). */
export const SHEET_BODY_PADDING_PX = 32;
/** The gap between the tree and the detail column (`gap-4`). */
export const SPAN_GRID_GAP_PX = 16;
/** The Sheet's left border. */
export const SHEET_BORDER_PX = 1;
/** Room for a classic (non-overlay) vertical scrollbar in the Sheet body. */
export const SCROLLBAR_ALLOWANCE_PX = 16;
/** Everything in the Sheet's width that is not tree or detail. */
export const TRACE_SHEET_CHROME_PX = SHEET_BODY_PADDING_PX + SPAN_GRID_GAP_PX + SHEET_BORDER_PX + SCROLLBAR_ALLOWANCE_PX;
/** The trace Sheet is never narrower than the tree and detail side by side (when the window allows). */
export const TRACE_SHEET_MIN_PX = SPAN_TREE_MIN_PX + SPAN_DETAIL_MIN_PX + TRACE_SHEET_CHROME_PX;
/** How much of the list stays visible left of the trace Sheet (its first columns: time and subject). */
export const TRACE_LIST_PEEK_PX = 280;
/** The log Sheet's preferred width: one record and its attributes. */
export const LOG_SHEET_PREFERRED_PX = 480;
/** The log Sheet's minimum (when the window allows). */
export const LOG_SHEET_MIN_PX = 360;

/**
 * The trace Sheet's width in px, or null for full width (expanded, or a window below 768 px). It
 * covers the main card except the list's first columns (TRACE_LIST_PEEK_PX), the sidebar staying
 * visible; never below TRACE_SHEET_MIN_PX, never wider than the window.
 */
export function traceSheetWidth(frame: FrameSize, expanded = false): number | null {
  if (expanded || frame.viewport < SHEET_FULL_WIDTH_BELOW_PX) return null;
  const room = frame.viewport - sidebarWidth(frame.sidebarCollapsed) - FRAME_GUTTER_PX - TRACE_LIST_PEEK_PX;
  return Math.min(frame.viewport, Math.max(TRACE_SHEET_MIN_PX, Math.round(room)));
}

/** The log Sheet's width in px, or null for full width (a window below 768 px). */
export function logSheetWidth(frame: FrameSize): number | null {
  if (frame.viewport < SHEET_FULL_WIDTH_BELOW_PX) return null;
  const room = frame.viewport - sidebarWidth(frame.sidebarCollapsed) - FRAME_GUTTER_PX;
  return Math.min(frame.viewport, Math.max(LOG_SHEET_MIN_PX, Math.min(LOG_SHEET_PREFERRED_PX, Math.round(room))));
}

/** The width the Sheet actually renders at: its own width, or the window when full width. */
export function renderedSheetWidth(frame: FrameSize, width: number | null): number {
  return width ?? frame.viewport;
}

/**
 * The span detail sits beside the tree when the Sheet has room for both minimums plus its chrome;
 * else below it. The grid beside uses the same minimums (`SPAN_GRID_BESIDE_CLASS`), so beside never
 * squeezes the detail under SPAN_DETAIL_MIN_PX.
 */
export function spanDetailBeside(sheetWidth: number): boolean {
  return sheetWidth >= TRACE_SHEET_MIN_PX;
}

/** The tree and detail columns side by side, never under their minimums (3 : 2 above them). */
export const SPAN_GRID_BESIDE_CLASS = "grid-cols-[minmax(360px,3fr)_minmax(320px,2fr)]";

export interface PrevNext {
  prev: string | null;
  next: string | null;
  /** 1-based position in the list, or null when the run is not in it. */
  position: number | null;
  total: number;
}

/** The runs before and after `current` in the hosting list's order (null at the ends or when absent). */
export function prevNext(runIds: readonly string[], current: string | null): PrevNext {
  const index = current === null ? -1 : runIds.indexOf(current);
  if (index === -1) return { prev: null, next: null, position: null, total: runIds.length };
  return {
    prev: index > 0 ? runIds[index - 1] : null,
    next: index < runIds.length - 1 ? runIds[index + 1] : null,
    position: index + 1,
    total: runIds.length,
  };
}
