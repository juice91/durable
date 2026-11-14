"use client";

import { useFrame } from "@/components/frame/FrameContext";
import { useSidePanel } from "@/components/frame/SidePanel";
import { clampSidePanelWidth } from "@/components/frame/frameStorage";
import { useIsWide, useViewportWidth } from "@/components/frame/useMediaQuery";
import { EDITOR_NAV_FIT_PX, editorLayout, type EditorLayout } from "./editorFit";

/**
 * The stepper layout for the window right now (`editorFit.ts`), following resizes, the sidebar rail
 * and the Config panel. The server and the hydration pass assume a 1280 px window.
 */
export function useEditorLayout(): EditorLayout {
  const isWide = useIsWide();
  const viewport = useViewportWidth(true);
  const sidebarCollapsed = useFrame()?.sidebarCollapsed ?? false;
  const panel = useSidePanel();
  const frame = { viewport, sidebarCollapsed };
  const panelWidth = isWide && panel.open ? clampSidePanelWidth(panel.width, viewport, { sidebarCollapsed, mainMinWidth: EDITOR_NAV_FIT_PX }) : null;
  return editorLayout({ frame, isWide, panelWidth });
}
