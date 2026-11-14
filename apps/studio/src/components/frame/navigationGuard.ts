"use client";

import { useCallback, useEffect, useRef } from "react";
import { useRouter } from "next/navigation";

/**
 * Frame-level navigation guard (SL.11 review I2). A page with something to lose (the agent editor's
 * unsaved edits) registers a guard; every navigator that moves by code (the `g` shortcuts, the ⌘K
 * palette, the breadcrumb switchers) asks `confirmNavigation(href)` before `router.push`. Link clicks
 * are held by the page's own capture listener (`useUnsavedChangesGuard`).
 *
 * The registry is module state rather than a React context so the navigators (mounted in the
 * frame, above any page) and the page reach the same guard without a provider. Guards are asked
 * newest first; the first refusal stops the navigation; with none registered, it goes ahead.
 *
 * Not covered: the browser's Back and Forward buttons (popstate). Next's app router gives no
 * cancellable hook for them; `beforeunload` only covers leaving the app. Deferred (plan, SL.11 notes).
 */
export type NavigationGuard = (href: string) => Promise<boolean> | boolean;

let guards: NavigationGuard[] = [];

/** Registers `guard`; returns the unregister function. */
export function registerNavigationGuard(guard: NavigationGuard): () => void {
  guards = [...guards, guard];
  return () => {
    guards = guards.filter((item) => item !== guard);
  };
}

/**
 * Whether navigation to `href` may go ahead: asks every registered guard, newest first, and stops at
 * the first that refuses (or throws). True when none refuses.
 */
export async function confirmNavigation(href: string): Promise<boolean> {
  for (const guard of [...guards].reverse()) {
    try {
      if (!(await guard(href))) return false;
    } catch {
      return false;
    }
  }
  return true;
}

/** Registers `guard` while it is non-null (the latest function is always called). */
export function useRegisterNavigationGuard(guard: NavigationGuard | null): void {
  const ref = useRef(guard);
  ref.current = guard;
  const active = guard !== null;
  useEffect(() => {
    if (!active) return;
    return registerNavigationGuard((href) => (ref.current ? ref.current(href) : true));
  }, [active]);
}

export interface NavigationGuardControls {
  /** Asks the active guard. */
  confirm: (href: string) => Promise<boolean>;
  /** Asks, then `router.push(href)` when allowed. Resolves to whether it navigated. */
  push: (href: string) => Promise<boolean>;
}

/** For navigators: confirm (or confirm and push) through the active guard. */
export function useNavigationGuard(): NavigationGuardControls {
  const router = useRouter();
  const routerRef = useRef(router);
  routerRef.current = router;
  const push = useCallback(async (href: string) => {
    if (!(await confirmNavigation(href))) return false;
    routerRef.current.push(href);
    return true;
  }, []);
  return { confirm: confirmNavigation, push };
}

/**
 * For navigators: runs `push(href)` at once when no guard is registered (so a plain navigation
 * stays synchronous), else after the active guard allows it.
 */
export function navigateGuarded(href: string, push: (href: string) => void): void {
  if (guards.length === 0) {
    push(href);
    return;
  }
  void confirmNavigation(href).then((ok) => {
    if (ok) push(href);
  });
}
