"use client";

import { useCallback, useSyncExternalStore } from "react";

/** The frame's desktop breakpoint: the sidebar is a column from here up, a drawer below. */
export const WIDE_QUERY = "(min-width: 1024px)";

function mediaList(query: string): MediaQueryList | null {
  try {
    return typeof window !== "undefined" && typeof window.matchMedia === "function" ? window.matchMedia(query) : null;
  } catch {
    return null;
  }
}

/**
 * Whether `query` matches, following changes live. The server (and the hydration pass) uses
 * `serverValue`, so markup that depends on it should also be correct by CSS alone.
 */
export function useMediaQuery(query: string, serverValue = true): boolean {
  const subscribe = useCallback(
    (onChange: () => void) => {
      const list = mediaList(query);
      if (!list) return () => {};
      if (typeof list.addEventListener === "function") {
        list.addEventListener("change", onChange);
        return () => list.removeEventListener("change", onChange);
      }
      // Older Safari.
      list.addListener?.(onChange);
      return () => list.removeListener?.(onChange);
    },
    [query],
  );
  const getSnapshot = useCallback(() => mediaList(query)?.matches ?? serverValue, [query, serverValue]);
  return useSyncExternalStore(subscribe, getSnapshot, () => serverValue);
}

/** Whether `query` matches right now (false where matchMedia is unavailable). Not for rendering. */
export function matchesNow(query: string): boolean {
  return mediaList(query)?.matches ?? false;
}

/** True at 1024 px and wider. */
export function useIsWide(): boolean {
  return useMediaQuery(WIDE_QUERY, true);
}
