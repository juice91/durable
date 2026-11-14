"use client";

import React, { createContext, useCallback, useContext, useEffect, useMemo, useState, useSyncExternalStore } from "react";
import { applySidebarAttribute, SIDEBAR_ATTRIBUTE, sidebarAttributeCollapsed } from "@/lib/sidebarPref";
import { readSidebarState, writeSidebarState } from "./frameStorage";
import { shouldHandleShortcut } from "./keyboard";
import { useIsWide } from "./useMediaQuery";

export interface FrameContextValue {
  /** The desktop sidebar is the 56 px icon rail. Persisted in `localStorage["studio.sidebar"]`. */
  sidebarCollapsed: boolean;
  setSidebarCollapsed: (collapsed: boolean) => void;
  toggleSidebar: () => void;
  /** The navigation drawer below 1024 px. */
  navOpen: boolean;
  setNavOpen: (open: boolean) => void;
  /** True at 1024 px and wider. */
  isWide: boolean;
  /** Opens the command palette, once one is registered (SL.5); null until then. */
  openPalette: (() => void) | null;
  /** The palette registers its opener here; the returned function unregisters it. */
  registerPalette: (open: () => void) => () => void;
}

const FrameContext = createContext<FrameContextValue | null>(null);

// The collapsed state lives on <html data-sidebar> (set before paint by the head script). A client
// render reads it straight away. When hydrating, React's first pass uses the server snapshot
// (expanded, to match the server markup) and then re-renders as the rail; the CSS keyed on the
// attribute already shows the rail's shape, so the switch is not visible. Writes go through
// `setCollapsed`, which notifies synchronously; a MutationObserver catches other changes.
const sidebarListeners = new Set<() => void>();
function notifySidebar() {
  sidebarListeners.forEach((listener) => listener());
}
function subscribeSidebar(listener: () => void) {
  sidebarListeners.add(listener);
  let observer: MutationObserver | null = null;
  if (typeof MutationObserver !== "undefined") {
    observer = new MutationObserver(listener);
    observer.observe(document.documentElement, { attributes: true, attributeFilter: [SIDEBAR_ATTRIBUTE] });
  }
  return () => {
    sidebarListeners.delete(listener);
    observer?.disconnect();
  };
}
const getSidebarCollapsed = () => sidebarAttributeCollapsed();
const getServerSidebarCollapsed = () => false;

function setCollapsed(collapsed: boolean) {
  applySidebarAttribute(collapsed);
  notifySidebar();
}

/** The frame's state, or null outside the app frame (a page rendered alone in a test). */
export function useFrame(): FrameContextValue | null {
  return useContext(FrameContext);
}

export function FrameProvider({ children }: { children: React.ReactNode }) {
  const sidebarCollapsed = useSyncExternalStore(subscribeSidebar, getSidebarCollapsed, getServerSidebarCollapsed);
  const [navOpen, setNavOpen] = useState(false);
  const [palette, setPalette] = useState<{ open: () => void } | null>(null);
  const isWide = useIsWide();

  // Storage is the persisted value; the attribute mirrors it. The head script normally did this
  // already; this covers a missing or blocked script (and tests, where no head script runs).
  useEffect(() => {
    const stored = readSidebarState() === "collapsed";
    if (stored !== sidebarAttributeCollapsed()) setCollapsed(stored);
  }, []);

  const setSidebarCollapsed = useCallback((collapsed: boolean) => {
    setCollapsed(collapsed);
    writeSidebarState(collapsed ? "collapsed" : "expanded");
  }, []);

  const toggleSidebar = useCallback(() => setSidebarCollapsed(!sidebarAttributeCollapsed()), [setSidebarCollapsed]);

  // The drawer belongs to narrow screens: widening the window closes it.
  useEffect(() => {
    if (isWide) setNavOpen(false);
  }, [isWide]);

  // `[` toggles the rail on wide screens (ignored while typing, with modifiers or under a modal).
  useEffect(() => {
    function onKeyDown(event: KeyboardEvent) {
      if (event.key !== "[" || !isWide || !shouldHandleShortcut(event)) return;
      event.preventDefault();
      toggleSidebar();
    }
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [isWide, toggleSidebar]);

  const registerPalette = useCallback((open: () => void) => {
    const entry = { open };
    setPalette(entry);
    return () => setPalette((current) => (current === entry ? null : current));
  }, []);

  const value = useMemo<FrameContextValue>(
    () => ({ sidebarCollapsed, setSidebarCollapsed, toggleSidebar, navOpen, setNavOpen, isWide, openPalette: palette?.open ?? null, registerPalette }),
    [sidebarCollapsed, setSidebarCollapsed, toggleSidebar, navOpen, isWide, palette, registerPalette],
  );
  return <FrameContext.Provider value={value}>{children}</FrameContext.Provider>;
}
