/**
 * What the frame keeps in localStorage. Every access is wrapped: blocked or throwing storage
 * (private mode, quota) falls back to the defaults.
 */
import { SIDEBAR_STORAGE_KEY } from "@/lib/sidebarPref";

export { SIDEBAR_STORAGE_KEY };
export const SIDE_PANEL_STORAGE_KEY = "studio.sidepanel.v1";

export type SidebarState = "expanded" | "collapsed";

export const SIDE_PANEL_MIN_WIDTH = 360;
export const SIDE_PANEL_DEFAULT_WIDTH = 400;
/** The panel never takes more than this share of the window. */
export const SIDE_PANEL_MAX_FRACTION = 0.5;

function storage(): Storage | null {
  try {
    return typeof window === "undefined" ? null : window.localStorage;
  } catch {
    return null;
  }
}

export function readSidebarState(): SidebarState {
  try {
    return storage()?.getItem(SIDEBAR_STORAGE_KEY) === "collapsed" ? "collapsed" : "expanded";
  } catch {
    return "expanded";
  }
}

export function writeSidebarState(state: SidebarState): void {
  try {
    storage()?.setItem(SIDEBAR_STORAGE_KEY, state);
  } catch {
    // Not saved: the sidebar reopens expanded next time.
  }
}

/** The widest the side panel may be for a window `viewportWidth` px wide (never below the minimum). */
export function sidePanelMaxWidth(viewportWidth: number): number {
  return Math.max(SIDE_PANEL_MIN_WIDTH, Math.floor(viewportWidth * SIDE_PANEL_MAX_FRACTION));
}

export function clampSidePanelWidth(width: number, viewportWidth: number): number {
  if (!Number.isFinite(width)) return SIDE_PANEL_DEFAULT_WIDTH;
  return Math.round(Math.min(Math.max(width, SIDE_PANEL_MIN_WIDTH), sidePanelMaxWidth(viewportWidth)));
}

/** The saved width (`{"width": n}`), or the default when absent, unreadable or malformed. */
export function readSidePanelWidth(): number {
  try {
    const raw = storage()?.getItem(SIDE_PANEL_STORAGE_KEY);
    if (!raw) return SIDE_PANEL_DEFAULT_WIDTH;
    const parsed: unknown = JSON.parse(raw);
    const width = parsed && typeof parsed === "object" ? (parsed as { width?: unknown }).width : undefined;
    return typeof width === "number" && Number.isFinite(width) && width > 0 ? width : SIDE_PANEL_DEFAULT_WIDTH;
  } catch {
    return SIDE_PANEL_DEFAULT_WIDTH;
  }
}

export function writeSidePanelWidth(width: number): void {
  try {
    storage()?.setItem(SIDE_PANEL_STORAGE_KEY, JSON.stringify({ width: Math.round(width) }));
  } catch {
    // Not saved: the panel reopens at the default width.
  }
}
