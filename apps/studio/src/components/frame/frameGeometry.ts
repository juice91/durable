/**
 * The app frame's horizontal geometry at 1024 px and up (`AppShell`), as numbers, so pages can work
 * out how much room the main card has without measuring it. Keep in step with AppShell's classes:
 *
 *   [sidebar w-60 | w-14] [main card, border 1 px each side] gap-2 [resize handle w-2 -mx-1] gap-2 [side panel] p-2 (right)
 *
 * Pure: no window, no React. SSR-safe by construction.
 */

/** The expanded sidebar (`w-60`). */
export const SIDEBAR_WIDTH_PX = 240;
/** The collapsed icon rail (`w-14`). */
export const SIDEBAR_RAIL_WIDTH_PX = 56;
/** The padding right of the main card (or the side panel): `md:p-2 lg:pl-0`. */
export const FRAME_GUTTER_PX = 8;
/** The gap between the main card, the resize handle and the side panel (`gap-2`). */
export const FRAME_GAP_PX = 8;
/** The main card's left and right border. */
export const MAIN_CARD_BORDER_PX = 2;
/** The side panel's resize handle: `w-2` with `-mx-1`, so it adds no width of its own. */
export const SIDE_PANEL_HANDLE_PX = 0;

export interface FrameSize {
  /** `window.innerWidth`. */
  viewport: number;
  /** The sidebar is the 56 px rail. */
  sidebarCollapsed: boolean;
}

export function sidebarWidth(sidebarCollapsed: boolean): number {
  return sidebarCollapsed ? SIDEBAR_RAIL_WIDTH_PX : SIDEBAR_WIDTH_PX;
}

/**
 * The room the main card's content and an open side panel share: the window minus the sidebar, the
 * gutter, the card's border and the two gaps around the handle.
 */
export function mainAndPanelSpace(frame: FrameSize): number {
  return frame.viewport - sidebarWidth(frame.sidebarCollapsed) - FRAME_GUTTER_PX - MAIN_CARD_BORDER_PX - 2 * FRAME_GAP_PX - SIDE_PANEL_HANDLE_PX;
}

/** The main card's inner width with a side panel `panelWidth` px wide open (null: no panel). */
export function mainCardWidth(frame: FrameSize, panelWidth: number | null): number {
  if (panelWidth === null) return frame.viewport - sidebarWidth(frame.sidebarCollapsed) - FRAME_GUTTER_PX - MAIN_CARD_BORDER_PX;
  return mainAndPanelSpace(frame) - panelWidth;
}
