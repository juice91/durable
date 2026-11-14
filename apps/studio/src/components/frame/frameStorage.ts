/**
 * What the frame keeps in localStorage. Every access is wrapped: blocked or throwing storage
 * (private mode, quota) falls back to the defaults.
 */
import { SIDEBAR_STORAGE_KEY } from "@/lib/sidebarPref";
import { mainAndPanelSpace } from "./frameGeometry";
import { safeLocalStorage } from "@/lib/safeStorage";

export { SIDEBAR_STORAGE_KEY };
export const SIDE_PANEL_STORAGE_KEY = "studio.sidepanel.v1";

export type SidebarState = "expanded" | "collapsed";

export const SIDE_PANEL_MIN_WIDTH = 360;
export const SIDE_PANEL_DEFAULT_WIDTH = 400;
/** The panel never takes more than this share of the window. */
export const SIDE_PANEL_MAX_FRACTION = 0.5;

export function readSidebarState(): SidebarState {
  try {
    return safeLocalStorage()?.getItem(SIDEBAR_STORAGE_KEY) === "collapsed" ? "collapsed" : "expanded";
  } catch {
    return "expanded";
  }
}

export function writeSidebarState(state: SidebarState): void {
  try {
    safeLocalStorage()?.setItem(SIDEBAR_STORAGE_KEY, state);
  } catch {
    // Not saved: the sidebar reopens expanded next time.
  }
}

export interface SidePanelClampOptions {
  /** The sidebar is the 56 px rail (more room for the panel). Default false. */
  sidebarCollapsed?: boolean;
  /**
   * The width the page needs to keep in its main card beside the panel (the agent Chat tab's
   * threads + chat minimums). The panel's maximum is then also capped at the space left after it.
   */
  mainMinWidth?: number;
}

/**
 * The widest the side panel may be for a window `viewportWidth` px wide: half the window and, when
 * the page reserves `mainMinWidth`, no wider than the frame's space minus that reservation. Never
 * below the minimum (where even the minimum does not fit, the page adapts: see the Chat tab).
 */
export function sidePanelMaxWidth(viewportWidth: number, options: SidePanelClampOptions = {}): number {
  let max = Math.floor(viewportWidth * SIDE_PANEL_MAX_FRACTION);
  if (options.mainMinWidth && options.mainMinWidth > 0) {
    const space = mainAndPanelSpace({ viewport: viewportWidth, sidebarCollapsed: options.sidebarCollapsed ?? false });
    max = Math.min(max, Math.floor(space - options.mainMinWidth));
  }
  return Math.max(SIDE_PANEL_MIN_WIDTH, max);
}

/** `width` within [minimum, `sidePanelMaxWidth`]; a non-finite width becomes the default. */
export function clampSidePanelWidth(width: number, viewportWidth: number, options: SidePanelClampOptions = {}): number {
  if (!Number.isFinite(width)) return SIDE_PANEL_DEFAULT_WIDTH;
  return Math.round(Math.min(Math.max(width, SIDE_PANEL_MIN_WIDTH), sidePanelMaxWidth(viewportWidth, options)));
}

/** The saved width (`{"width": n}`), or the default when absent, unreadable or malformed. */
export function readSidePanelWidth(): number {
  try {
    const raw = safeLocalStorage()?.getItem(SIDE_PANEL_STORAGE_KEY);
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
    safeLocalStorage()?.setItem(SIDE_PANEL_STORAGE_KEY, JSON.stringify({ width: Math.round(width) }));
  } catch {
    // Not saved: the panel reopens at the default width.
  }
}
