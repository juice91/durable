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

/** A desktop window wide enough for the agent Chat tab's threads and chat beside Config. */
export const TEST_WIDE_WIDTH = 1440;

/** Sets `window.innerWidth` (jsdom's default is 1024); returns the restore function. */
export function setWindowWidth(width: number): () => void {
  const descriptor = Object.getOwnPropertyDescriptor(window, "innerWidth");
  Object.defineProperty(window, "innerWidth", { configurable: true, writable: true, value: width });
  return () => {
    if (descriptor) Object.defineProperty(window, "innerWidth", descriptor);
  };
}

/**
 * Replaces `window.matchMedia` so `(min-width: 1024px)` matches when `wide`; every other query
 * (the OS dark preference) does not match. It does not touch `window.innerWidth` (jsdom: 1024 px):
 * a test of width-dependent layout (the agent Chat tab beside Config) sets it with `setWindowWidth`.
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
