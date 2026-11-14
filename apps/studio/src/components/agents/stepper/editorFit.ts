/**
 * Which stepper layout fits the main card (the decision "Editor as a stepped form", with the
 * agent page's shared pane fit rule, review I3): the frame's geometry (`frameGeometry.ts`), the side
 * panel when Config is open, and the same fit margin as the Chat tab (`chatFit.ts`). Pure, so it is
 * unit-tested with injected widths (jsdom does no layout).
 *
 * - "columns": step nav | step | versions column, when all three fit at their widths;
 * - "nav": step nav | step, versions in a header "Versions" Sheet;
 * - "compact": a step select above the step, versions in the Sheet (phones, or no room for the nav).
 */
import { CHAT_FIT_MARGIN_PX } from "@/components/agentPage/chatFit";
import { mainCardWidth, narrowMainWidth, type FrameSize } from "@/components/frame/frameGeometry";

/** The step nav column. */
export const STEP_NAV_PX = 220;
/** The versions column. */
export const VERSIONS_COLUMN_PX = 260;
/** The step form never gets narrower than this beside the columns. */
export const STEP_CONTENT_MIN_PX = 420;
/** Below this window width the nav is always a select (the decision's 768 px). */
export const STEP_SELECT_BELOW_PX = 768;
/** What the main card needs for each layout (minimums + the shared margin). */
export const EDITOR_COLUMNS_FIT_PX = STEP_NAV_PX + STEP_CONTENT_MIN_PX + VERSIONS_COLUMN_PX + CHAT_FIT_MARGIN_PX;
export const EDITOR_NAV_FIT_PX = STEP_NAV_PX + STEP_CONTENT_MIN_PX + CHAT_FIT_MARGIN_PX;

export type EditorLayout = "columns" | "nav" | "compact";

export interface EditorFitInput {
  frame: FrameSize;
  /** The frame's wide layout (1024 px and up): the sidebar is a column and Config may take one. */
  isWide: boolean;
  /** The side panel's rendered width when it is open as a column; null when closed (or a Sheet). */
  panelWidth: number | null;
}

/**
 * The main card's width: beside the sidebar and an open panel when wide; below that the window less
 * the card's gutter and border (`narrowMainWidth`; taking the whole window chose "columns" at
 * 916–933 px, where the card is 18 px narrower and the step was squeezed).
 */
export function editorMainWidth({ frame, isWide, panelWidth }: EditorFitInput): number {
  return isWide ? mainCardWidth(frame, panelWidth) : narrowMainWidth(frame.viewport);
}

export function editorLayout(input: EditorFitInput): EditorLayout {
  if (input.frame.viewport < STEP_SELECT_BELOW_PX) return "compact";
  const width = editorMainWidth(input);
  if (width >= EDITOR_COLUMNS_FIT_PX) return "columns";
  if (width >= EDITOR_NAV_FIT_PX) return "nav";
  return "compact";
}
