"use client";

import { useCallback, useEffect, useRef } from "react";
import { usePathname, useRouter, useSearchParams } from "next/navigation";

export interface UseUrlSheetOptions {
  /** Other query parameters removed when the Sheet closes (e.g. `tab` for `?trace=…&tab=events`). */
  clear?: string[];
  /**
   * History mode. "replace" (default): opening, stepping prev / next and closing do not add Back
   * entries. "push": opening adds one, and closing a Sheet this hook opened goes Back, so Back
   * never reopens it.
   */
  history?: "push" | "replace";
}

export interface UrlSheet {
  /** The parameter's value, or null when the Sheet is closed (absent or empty). */
  value: string | null;
  isOpen: boolean;
  /** Open (or switch) the Sheet to `value`; `extra` sets or removes (null) other parameters. */
  open: (value: string, extra?: Record<string, string | null>) => void;
  close: () => void;
  /** Pass to `<Sheet onOpenChange>`: closing removes the parameter. */
  onOpenChange: (open: boolean) => void;
}

/** Builds `pathname?query` from the current parameters with `changes` applied (null removes). */
export function withSearchParams(pathname: string, current: URLSearchParams | { toString(): string } | null, changes: Record<string, string | null>): string {
  const params = new URLSearchParams(current ? current.toString() : "");
  for (const [key, value] of Object.entries(changes)) {
    if (value === null) params.delete(key);
    else params.set(key, value);
  }
  const query = params.toString();
  return query ? `${pathname}?${query}` : pathname;
}

/**
 * Binds a Sheet to the query parameter `param`, so the open Sheet is part of a shareable URL.
 *
 * Uses `useSearchParams`, so in the Next app router the page (or the component calling this) must
 * sit inside a `<Suspense>` boundary, or a static build bails out of prerendering that route.
 */
export function useUrlSheet(param: string, { clear = [], history = "replace" }: UseUrlSheetOptions = {}): UrlSheet {
  const router = useRouter();
  const pathname = usePathname() ?? "";
  const searchParams = useSearchParams();
  const value = searchParams?.get(param) || null;
  const clearKey = clear.join(",");
  // The values this hook wrote since its push (the pushed one plus switches that replaced it); empty
  // when the open Sheet was not opened by this hook. Closing goes Back only while the URL shows one
  // of them: if a link or Back changed it, the history entry on top is not ours to pop.
  const written = useRef<Set<string>>(new Set());

  useEffect(() => {
    // Closed or changed from elsewhere (Back, a link): nothing of ours left to pop.
    if (value === null || !written.current.has(value)) written.current.clear();
  }, [value]);

  // Built from the live URL at call time: two calls in one tick (open then switch) must not both
  // start from the same, stale render's parameters. The hook's own parameters are the fallback
  // when the window is on another path (mid-navigation) or absent.
  const currentParams = useCallback((): URLSearchParams | null => {
    if (typeof window !== "undefined" && window.location.pathname === pathname) return new URLSearchParams(window.location.search);
    return searchParams ? new URLSearchParams(searchParams.toString()) : null;
  }, [pathname, searchParams]);

  const open = useCallback(
    (next: string, extra: Record<string, string | null> = {}) => {
      const href = withSearchParams(pathname, currentParams(), { ...extra, [param]: next });
      if (history === "push") {
        // Only the first open pushes; switching value while open replaces, so one Back closes.
        if (written.current.size > 0) router.replace(href, { scroll: false });
        else router.push(href, { scroll: false });
        written.current.add(next);
      } else {
        router.replace(href, { scroll: false });
      }
    },
    [currentParams, history, param, pathname, router],
  );

  const close = useCallback(() => {
    const ours = value !== null && written.current.has(value);
    written.current.clear();
    if (history === "push" && ours) {
      router.back();
      return;
    }
    const changes: Record<string, string | null> = { [param]: null };
    for (const key of clearKey ? clearKey.split(",") : []) changes[key] = null;
    router.replace(withSearchParams(pathname, currentParams(), changes), { scroll: false });
  }, [clearKey, currentParams, history, param, pathname, router, value]);

  const onOpenChange = useCallback(
    (next: boolean) => {
      if (!next) close();
    },
    [close],
  );

  return { value, isOpen: value !== null, open, close, onOpenChange };
}
