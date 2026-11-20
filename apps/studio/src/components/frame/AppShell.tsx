"use client";

import { PendingApprovalToasts } from "@/components/approvals/PendingApprovalToasts";
import React, { useCallback } from "react";
import { TooltipProvider } from "@/components/ui/Tooltip";
import { Toaster } from "@/components/ui/Toaster";
import { MAIN_ID } from "./brand";
import { FrameProvider } from "./FrameContext";
import { MobileNavDrawer, MobileTopBar } from "./MobileNav";
import { SidePanelProvider, SidePanelSlot } from "./SidePanel";
import { Sidebar } from "./Sidebar";

export { MAIN_ID };

/**
 * "Skip to content": first in the tab order, visible only when focused. Focus moves to <main>
 * explicitly, since not every browser moves focus on a same-page link.
 */
export function SkipLink() {
  const onClick = useCallback((event: React.MouseEvent<HTMLAnchorElement>) => {
    const main = document.getElementById(MAIN_ID);
    if (!main) return;
    event.preventDefault();
    main.focus();
    main.scrollTop = 0;
  }, []);
  return (
    <a
      href={`#${MAIN_ID}`}
      onClick={onClick}
      data-testid="skip-link"
      className="sr-only rounded-md bg-primary px-3 py-2 text-label font-medium text-primary-foreground focus:not-sr-only focus:fixed focus:left-2 focus:top-2 focus:z-[60] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
    >
      Skip to content
    </a>
  );
}

/**
 * The app frame (decision "App frame"): a full-height canvas holding the sidebar, and the page in a
 * raised main card with an 8 px gap around it (none below 768 px). Below 1024 px the sidebar becomes
 * a top bar plus a drawer. The route side panel, when a page opens one, sits right of the card.
 * Mounts the app-wide TooltipProvider and Toaster once.
 */
export function AppShell({ children }: { children: React.ReactNode }) {
  return (
    <TooltipProvider>
      <FrameProvider>
        <SidePanelProvider>
          <SkipLink />
          <div data-testid="app-shell" className="flex h-dvh min-h-0 flex-col bg-canvas text-foreground lg:flex-row">
            <MobileTopBar />
            <Sidebar />
            <div className="flex min-h-0 min-w-0 flex-1 gap-2 md:p-2 lg:pl-0">
              <main
                id={MAIN_ID}
                tabIndex={-1}
                data-testid="main-card"
                className="min-h-0 min-w-0 flex-1 overflow-auto bg-surface outline-none md:rounded-lg md:border md:border-border md:shadow-raised"
              >
                {children}
              </main>
              <SidePanelSlot />
            </div>
          </div>
          <MobileNavDrawer />
          <Toaster />
          <PendingApprovalToasts />
        </SidePanelProvider>
      </FrameProvider>
    </TooltipProvider>
  );
}
