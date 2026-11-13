/**
 * Test helpers for the app frame. Not imported by app code.
 */
import React from "react";
import { vi } from "vitest";
import { ThemeProvider } from "@/components/theme/ThemeProvider";
import { AppShell } from "./AppShell";

export interface MatchMediaControl {
  /** Change the viewport class and notify listeners (as a real resize across 1024 px would). */
  setWide: (wide: boolean) => void;
  restore: () => void;
}

/**
 * Replaces `window.matchMedia` so `(min-width: 1024px)` matches when `wide`; every other query
 * (the OS dark preference) does not match.
 */
export function mockMatchMedia(wide: boolean): MatchMediaControl {
  const original = window.matchMedia;
  let isWide = wide;
  const listeners = new Set<() => void>();
  window.matchMedia = vi.fn((query: string) => {
    const widthQuery = query.includes("min-width: 1024px");
    return {
      get matches() {
        return widthQuery ? isWide : false;
      },
      media: query,
      onchange: null,
      addEventListener: (_type: string, listener: () => void) => {
        if (widthQuery) listeners.add(listener);
      },
      removeEventListener: (_type: string, listener: () => void) => {
        listeners.delete(listener);
      },
      addListener: () => {},
      removeListener: () => {},
      dispatchEvent: () => false,
    } as unknown as MediaQueryList;
  }) as typeof window.matchMedia;
  return {
    setWide(next: boolean) {
      isWide = next;
      listeners.forEach((listener) => listener());
    },
    restore() {
      window.matchMedia = original;
    },
  };
}

/** The whole frame (theme, tooltips, sidebar, drawer, side panel) around `children`. */
export function FrameHarness({ children }: { children?: React.ReactNode }) {
  return (
    <ThemeProvider>
      <AppShell>{children}</AppShell>
    </ThemeProvider>
  );
}
