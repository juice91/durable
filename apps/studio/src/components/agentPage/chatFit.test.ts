import { describe, expect, test } from "vitest";
import { mainAndPanelSpace, mainCardWidth, SIDEBAR_RAIL_WIDTH_PX, SIDEBAR_WIDTH_PX } from "@/components/frame/frameGeometry";
import { clampSidePanelWidth, SIDE_PANEL_DEFAULT_WIDTH, SIDE_PANEL_MIN_WIDTH, sidePanelMaxWidth } from "@/components/frame/frameStorage";
import {
  CHAT_FIT_MARGIN_PX,
  CHAT_MIN_PX,
  CHAT_PANES_MIN_PX,
  chatPanelClamp,
  chatPanelWidth,
  chatPanesFit,
  configOpenByDefault,
  paneWidthFromLayout,
  THREADS_MIN_PX,
  threadsAutoCollapsed,
  threadsShown,
} from "./chatFit";
import * as panes from "./AgentChatPanes";

// jsdom does no layout: the fit rules are tested on injected widths. The frame's chrome at 1024 px+
// is sidebar + 8 (right gutter) + 2 (card border) + 2 x 8 (gaps around the panel's handle).
const expanded = (viewport: number) => ({ viewport, sidebarCollapsed: false });
const rail = (viewport: number) => ({ viewport, sidebarCollapsed: true });

describe("frame geometry", () => {
  test("the main card's width with and without the panel", () => {
    expect(mainCardWidth(expanded(1024), null)).toBe(1024 - SIDEBAR_WIDTH_PX - 8 - 2);
    expect(mainCardWidth(expanded(1024), 400)).toBe(1024 - 240 - 8 - 2 - 16 - 400); // 358: the review's squeeze
    expect(mainCardWidth(rail(1024), 400)).toBe(1024 - SIDEBAR_RAIL_WIDTH_PX - 8 - 2 - 16 - 400);
    expect(mainAndPanelSpace(expanded(1280))).toBe(1280 - 240 - 26);
  });
});

describe("pane minimums come from one place", () => {
  test("AgentChatPanes re-exports chatFit's constants", () => {
    expect(panes.THREADS_MIN_PX).toBe(THREADS_MIN_PX);
    expect(panes.CHAT_MIN_PX).toBe(CHAT_MIN_PX);
    expect(CHAT_PANES_MIN_PX).toBe(THREADS_MIN_PX + CHAT_MIN_PX);
    expect(CHAT_FIT_MARGIN_PX).toBe(16);
  });
});

describe("chatPanesFit", () => {
  test.each([
    // [frame, panel width, fits]
    [expanded(1024), 400, false],
    [expanded(1024), null, true],
    [expanded(1266), 400, false], // 600 px: the bare minimums, but not the 16 px margin
    [expanded(1281), 400, false], // 615 px
    [expanded(1282), 400, true], // 616 px: the expanded-sidebar breakpoint at the default width
    [expanded(1280), 400, false],
    [rail(1097), 400, false],
    [rail(1098), 400, true], // the rail's breakpoint at the default width
    [rail(1100), 400, true],
    [expanded(1440), 600, false],
    [expanded(1440), 558, true],
    [expanded(1440), 559, false],
  ] as const)("%o with a %s px panel fits: %s", (frame, panel, fits) => {
    expect(chatPanesFit(frame, panel)).toBe(fits);
  });
});

describe("configOpenByDefault: open only when it fits at the default width; a saved choice wins", () => {
  test("no saved choice", () => {
    expect(configOpenByDefault(null, expanded(1024))).toBe(false);
    expect(configOpenByDefault(null, expanded(1200))).toBe(false);
    expect(configOpenByDefault(null, expanded(1280))).toBe(false);
    expect(configOpenByDefault(null, expanded(1282))).toBe(true);
    expect(configOpenByDefault(null, rail(1100))).toBe(true);
  });
  test("a saved choice wins either way", () => {
    expect(configOpenByDefault(true, expanded(1024))).toBe(true);
    expect(configOpenByDefault(false, expanded(1920))).toBe(false);
  });
});

describe("the panel's maximum leaves threads + chat in the main card", () => {
  test("generic pages keep half the window", () => {
    expect(sidePanelMaxWidth(1600)).toBe(800);
    expect(sidePanelMaxWidth(500)).toBe(SIDE_PANEL_MIN_WIDTH);
  });
  test("the Chat tab caps at the space minus threads + chat", () => {
    // 1440 expanded: space 1174, minus 616 (minimums + margin) = 558 (< half the window, 720).
    expect(sidePanelMaxWidth(1440, chatPanelClamp(false))).toBe(558);
    // The rail frees 184 px: 742, but half the window (720) still caps it.
    expect(sidePanelMaxWidth(1440, chatPanelClamp(true))).toBe(720);
    // 1920: half the window (960) is below 1654 - 616 = 1038.
    expect(sidePanelMaxWidth(1920, chatPanelClamp(false))).toBe(960);
  });
  test("the minimum is the floor where nothing fits", () => {
    expect(sidePanelMaxWidth(1024, chatPanelClamp(false))).toBe(SIDE_PANEL_MIN_WIDTH);
    expect(clampSidePanelWidth(700, 1024, chatPanelClamp(false))).toBe(SIDE_PANEL_MIN_WIDTH);
  });
  test("a saved (or dragged) width is clamped, the default kept where it fits", () => {
    expect(chatPanelWidth(900, expanded(1440))).toBe(558);
    expect(chatPanelWidth(SIDE_PANEL_DEFAULT_WIDTH, expanded(1440))).toBe(SIDE_PANEL_DEFAULT_WIDTH);
    expect(chatPanesFit(expanded(1440), chatPanelWidth(900, expanded(1440)))).toBe(true);
  });
});

describe("threads auto-collapse", () => {
  const input = (viewport: number, configOpen: boolean, sidebarCollapsed = false, isWide = true) => {
    const frame = { viewport, sidebarCollapsed };
    return { isWide, configOpen, frame, panelWidth: chatPanelWidth(SIDE_PANEL_DEFAULT_WIDTH, frame) };
  };

  test("only with Config open where threads + chat do not fit", () => {
    expect(threadsAutoCollapsed(input(1024, true))).toBe(true);
    expect(threadsAutoCollapsed(input(1024, false))).toBe(false);
    expect(threadsAutoCollapsed(input(1440, true))).toBe(false);
    // Collapsing the sidebar to the rail returns the room at 1100 px.
    expect(threadsAutoCollapsed(input(1100, true))).toBe(true);
    expect(threadsAutoCollapsed(input(1100, true, true))).toBe(false);
    // Below 1024 px the threads are a Sheet: never auto-collapsed.
    expect(threadsAutoCollapsed(input(900, true, false, false))).toBe(false);
  });

  test("with threads collapsed, the chat keeps its minimum at 1024 px", () => {
    const frame = expanded(1024);
    expect(mainCardWidth(frame, chatPanelWidth(SIDE_PANEL_DEFAULT_WIDTH, frame))).toBeGreaterThanOrEqual(CHAT_MIN_PX);
  });

  test("what the pane shows: the user's choice unless auto-collapsed", () => {
    expect(threadsShown(true, false)).toBe(true);
    expect(threadsShown(true, true)).toBe(false);
    expect(threadsShown(false, false)).toBe(false);
    expect(threadsShown(false, true)).toBe(false);
  });
});

describe("paneWidthFromLayout: a pane's px from the reported layout", () => {
  test("percent of the group's width minus its separators", () => {
    expect(paneWidthFromLayout(39, 774)).toBe(302);
    expect(paneWidthFromLayout(50, 801, 1)).toBe(400);
    expect(paneWidthFromLayout(36.18, 774)).toBe(280);
  });
  test("null without a size or a number", () => {
    expect(paneWidthFromLayout(39, 0)).toBeNull();
    expect(paneWidthFromLayout(39, 1, 1)).toBeNull();
    expect(paneWidthFromLayout(Number.NaN, 774)).toBeNull();
  });
});
