"use client";

import React, { useCallback, useEffect, useRef } from "react";
import { usePathname } from "next/navigation";
import * as DialogPrimitive from "@radix-ui/react-dialog";
import { Menu, Search, X } from "lucide-react";
import { cn } from "@/lib/cn";
import { ICON_STROKE_WIDTH } from "@/lib/icons";
import { overlayClass } from "@/components/ui/Dialog";
import { IconButton } from "@/components/ui/IconButton";
import { Kbd } from "@/components/ui/Kbd";
import { Button } from "@/components/ui/Button";
import { useFocusReturn } from "@/components/ui/focusReturn";
import { useFrame } from "./FrameContext";
import { MAIN_ID, PRODUCT_NAME } from "./brand";
import { Monogram, SidebarNav } from "./Sidebar";

export const MOBILE_NAV_ID = "studio-mobile-nav";

/**
 * The top bar below 1024 px: the menu button (opens the navigation drawer), the product name and,
 * once the command palette exists (SL.5), a search button. Hidden by CSS from 1024 px.
 */
export function MobileTopBar() {
  const frame = useFrame();
  return (
    <header data-testid="mobile-top-bar" className="flex h-12 shrink-0 items-center gap-2 border-b border-border bg-canvas px-2 lg:hidden">
      <IconButton
        label="Open navigation"
        aria-expanded={frame?.navOpen ?? false}
        aria-controls={MOBILE_NAV_ID}
        tooltip={false}
        onClick={() => frame?.setNavOpen(true)}
        data-testid="mobile-nav-open"
        icon={<Menu strokeWidth={ICON_STROKE_WIDTH} aria-hidden="true" />}
      />
      <Monogram />
      <span className="min-w-0 flex-1 truncate text-label font-semibold text-foreground">{PRODUCT_NAME}</span>
      {frame?.openPalette ? (
        <Button variant="ghost" size="sm" onClick={frame.openPalette} aria-keyshortcuts="Meta+K Control+K" data-testid="mobile-search" className="gap-1.5 text-muted-foreground">
          <Search className="size-icon" strokeWidth={ICON_STROKE_WIDTH} aria-hidden="true" />
          <span className="sr-only sm:not-sr-only">Search…</span>
          <Kbd className="hidden sm:inline-flex">⌘K</Kbd>
        </Button>
      ) : null}
    </header>
  );
}

/**
 * The navigation drawer below 1024 px: a modal from the left with the same groups as the sidebar.
 * Focus is trapped while open and returns to the menu button; it closes on Escape, on the overlay,
 * on following a link and when the route changes.
 */
export function MobileNavDrawer() {
  const frame = useFrame();
  const pathname = usePathname();
  const open = frame?.navOpen ?? false;
  const setNavOpen = frame?.setNavOpen;
  // Set when a nav link closes the drawer: focus then goes to the new page's <main>, not back to
  // the menu button (Escape and the overlay still return focus to the button).
  const focusMainOnClose = useRef(false);
  const onCloseAutoFocus = useCallback((event: Event) => {
    if (!focusMainOnClose.current) return;
    focusMainOnClose.current = false;
    const main = document.getElementById(MAIN_ID);
    if (!main) return;
    event.preventDefault();
    main.focus();
  }, []);
  const focus = useFocusReturn({ onCloseAutoFocus });

  // Close on navigation (also covers Back and links outside the drawer).
  const lastPath = useRef(pathname);
  useEffect(() => {
    if (lastPath.current === pathname) return;
    lastPath.current = pathname;
    setNavOpen?.(false);
  }, [pathname, setNavOpen]);

  if (!frame) return null;
  return (
    <DialogPrimitive.Root open={open} onOpenChange={frame.setNavOpen}>
      <DialogPrimitive.Portal>
        <DialogPrimitive.Overlay className={cn(overlayClass, "lg:hidden")} />
        <DialogPrimitive.Content
          id={MOBILE_NAV_ID}
          aria-describedby={undefined}
          data-testid="mobile-nav-drawer"
          onOpenAutoFocus={focus.onOpenAutoFocus}
          onCloseAutoFocus={focus.onCloseAutoFocus}
          className="fixed inset-y-0 left-0 z-50 flex h-dvh w-72 max-w-[85vw] flex-col border-r border-border bg-canvas text-foreground shadow-overlay outline-none lg:hidden"
        >
          <div className="flex h-12 shrink-0 items-center gap-2 border-b border-border px-3">
            <Monogram />
            <span className="min-w-0 flex-1 truncate text-label font-semibold text-foreground">{PRODUCT_NAME}</span>
            <DialogPrimitive.Title className="sr-only">Navigation</DialogPrimitive.Title>
            <DialogPrimitive.Close asChild>
              <IconButton label="Close navigation" size="sm" tooltip={false} data-testid="mobile-nav-close" icon={<X strokeWidth={ICON_STROKE_WIDTH} aria-hidden="true" />} />
            </DialogPrimitive.Close>
          </div>
          <SidebarNav
            label="Main"
            onNavigate={() => {
              focusMainOnClose.current = true;
              frame.setNavOpen(false);
            }}
          />
        </DialogPrimitive.Content>
      </DialogPrimitive.Portal>
    </DialogPrimitive.Root>
  );
}
