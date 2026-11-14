/**
 * Does the agent Chat tab fit beside the Config side panel? (Review I3.) The threads pane and the
 * chat have minimum widths; the frame (sidebar, gaps, the panel) takes the rest of the window. These
 * are the single source of the pane minimums and the pure decisions built on them, so they can be
 * unit-tested with injected widths (jsdom does no layout).
 *
 * Policy:
 * - fits: the main card's width with the panel open >= THREADS_MIN_PX + CHAT_MIN_PX + CHAT_FIT_MARGIN_PX.
 * - Config opens by default on Chat only when it fits with the panel at its default width; a saved
 *   per-agent choice wins either way.
 * - The panel's maximum width leaves CHAT_PANES_FIT_PX in the main card (floored at the panel's
 *   own minimum): `chatPanelClamp` for `clampSidePanelWidth`.
 * - With Config open where it does not fit, the threads pane collapses automatically so the chat
 *   keeps CHAT_MIN_PX. That collapse is not the user's layout: it is never saved, and the threads
 *   come back when Config closes or the room returns, unless the user collapsed them.
 */
import { mainCardWidth, type FrameSize } from "@/components/frame/frameGeometry";
import { clampSidePanelWidth, SIDE_PANEL_DEFAULT_WIDTH, type SidePanelClampOptions } from "@/components/frame/frameStorage";

/** Threads pane: 280 px by default, 240 px at least, collapsible. */
export const THREADS_DEFAULT_PX = 280;
export const THREADS_MIN_PX = 240;
/** The chat never gets narrower than this beside the threads pane. */
export const CHAT_MIN_PX = 360;
/** What the main card needs for threads and chat side by side. */
export const CHAT_PANES_MIN_PX = THREADS_MIN_PX + CHAT_MIN_PX;
/**
 * Slack on top of the panes' minimums before "fits": the geometry is computed, not measured, so a
 * classic (non-overlay) scrollbar or a rounding pixel must not leave the kit refusing an expand
 * that the numbers allowed.
 */
export const CHAT_FIT_MARGIN_PX = 16;
/** What the main card must have for the panes to count as fitting (minimums + margin). */
export const CHAT_PANES_FIT_PX = CHAT_PANES_MIN_PX + CHAT_FIT_MARGIN_PX;

/** The side-panel clamp options for the Chat tab (the panel leaves room for both panes). */
export function chatPanelClamp(sidebarCollapsed: boolean): SidePanelClampOptions {
  return { sidebarCollapsed, mainMinWidth: CHAT_PANES_FIT_PX };
}

/** The panel's rendered width on the Chat tab for a saved width `savedWidth`. */
export function chatPanelWidth(savedWidth: number, frame: FrameSize): number {
  return clampSidePanelWidth(savedWidth, frame.viewport, chatPanelClamp(frame.sidebarCollapsed));
}

/** Threads and chat both fit (minimums + margin) beside a panel `panelWidth` px wide (null: closed). */
export function chatPanesFit(frame: FrameSize, panelWidth: number | null): boolean {
  return mainCardWidth(frame, panelWidth) >= CHAT_PANES_FIT_PX;
}

/**
 * Whether Config starts open on the Chat tab: the user's saved per-agent choice, else open only when
 * the panes fit beside the panel at its default width.
 */
export function configOpenByDefault(saved: boolean | null, frame: FrameSize): boolean {
  if (saved !== null) return saved;
  return chatPanesFit(frame, SIDE_PANEL_DEFAULT_WIDTH);
}

export interface ThreadsAutoCollapseInput {
  /** The frame is in its wide layout (1024 px and up); below that threads are a Sheet. */
  isWide: boolean;
  /** Config is open (as a column). */
  configOpen: boolean;
  frame: FrameSize;
  /** The panel's rendered width (`chatPanelWidth`). */
  panelWidth: number;
}

/** The threads pane is collapsed automatically (not by the user) to keep the chat at CHAT_MIN_PX. */
export function threadsAutoCollapsed({ isWide, configOpen, frame, panelWidth }: ThreadsAutoCollapseInput): boolean {
  return isWide && configOpen && !chatPanesFit(frame, panelWidth);
}

/** What the threads pane shows: the user's choice, unless it is auto-collapsed. */
export function threadsShown(userOpen: boolean, autoCollapsed: boolean): boolean {
  return userOpen && !autoCollapsed;
}

/**
 * A pane's width in px from a layout's percentage. The kit's percentages are of the panels' total
 * width, which is the group's width minus its separators. Computed from the layout the kit reports
 * (not measured from the DOM, which still shows the previous layout while `onLayoutChanged` runs).
 * Null when the group has no size.
 */
export function paneWidthFromLayout(percent: number, groupWidth: number, separatorsWidth = 0): number | null {
  const available = groupWidth - separatorsWidth;
  if (!Number.isFinite(percent) || !(available > 0)) return null;
  return Math.round((percent / 100) * available);
}

/**
 * A threads width converted from a stored layout, kept usable: at least THREADS_MIN_PX, and never
 * so wide that the chat drops under CHAT_MIN_PX in a group `groupWidth` px wide.
 */
export function clampThreadsWidth(width: number, groupWidth: number, separatorsWidth = 0): number {
  const max = groupWidth - separatorsWidth - CHAT_MIN_PX;
  return Math.round(Math.max(THREADS_MIN_PX, Math.min(width, max)));
}
