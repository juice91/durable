"use client";

import React, { useCallback, useEffect, useRef, useState } from "react";
import { usePathname, useRouter } from "next/navigation";
import { ConfirmDialog } from "@/components/shared/ConfirmDialog";
import { useRegisterNavigationGuard } from "@/components/frame/navigationGuard";
import { isSafeTarget } from "@/lib/routes";

/** Marks a link that calls the navigation guard itself; the document click guard leaves it alone. */
export const GUARD_SELF_ATTRIBUTE = "data-guard-self";

/**
 * The in-app link a click would follow away from `pathname`, or null: same-origin `<a href>`s to
 * another path, followed by a plain left click without a target. Links within the editor's own
 * path (`?step=`, `?compare=`) keep the edits, so they are not guarded.
 */
export function guardedHref(event: Pick<MouseEvent, "button" | "metaKey" | "ctrlKey" | "shiftKey" | "altKey" | "defaultPrevented" | "target">, pathname: string, origin: string): string | null {
  if (event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return null;
  const element = event.target instanceof Element ? event.target.closest("a[href]") : null;
  if (!(element instanceof HTMLAnchorElement)) return null;
  // A link that runs the navigation guard itself (a menu item that must close first).
  if (element.hasAttribute(GUARD_SELF_ATTRIBUTE)) return null;
  if ((element.target && element.target !== "_self") || element.hasAttribute("download")) return null;
  let url: URL;
  try {
    url = new URL(element.getAttribute("href") ?? "", origin + pathname);
  } catch {
    return null;
  }
  if (url.origin !== origin || url.pathname === pathname) return null;
  // What the guard would `router.push`: only a same-origin path (`//host/…` would leave the site).
  const target = url.pathname + url.search + url.hash;
  return isSafeTarget(target) ? target : null;
}

/** Whether `href` leaves the page at `pathname` (another path; `?step=` and `?compare=` stay). */
export function leavesPage(href: string, pathname: string, origin: string): boolean {
  try {
    const url = new URL(href, origin + pathname);
    return url.origin !== origin || url.pathname !== pathname;
  } catch {
    return true;
  }
}

/** The guard dialog's words. */
export interface UnsavedChangesCopy {
  title: string;
  description: string;
  confirmLabel: string;
}

const EDITOR_COPY: UnsavedChangesCopy = {
  title: "Leave without saving?",
  description: "The editor has changes that are not saved. Leaving discards them; Save draft keeps them.",
  confirmLabel: "Leave without saving",
};

/**
 * "Unsaved changes" guard for the editor. While `dirty`:
 * - closing or reloading the tab asks the browser's own question (`beforeunload`);
 * - a plain click on an in-app link to another page (the agent tabs, the sidebar, Test draft, a
 *   Config link to another agent) is held (`preventDefault` only, so a menu holding the link still
 *   closes) and a confirm dialog asks first; "Leave without saving" follows the link;
 * - navigation by code (the `g` shortcuts, the ⌘K palette, the breadcrumb switcher) asks the same
 *   dialog through the frame's navigation guard (`components/frame/navigationGuard.ts`).
 * Not covered: the browser's Back and Forward buttons (no cancellable app-router hook; deferred).
 * Returns the dialog to render. `copy` replaces the dialog's words (the memory inspector's working
 * memory edit uses it, RM.18).
 */
export function useUnsavedChangesGuard(dirty: boolean, copy: UnsavedChangesCopy = EDITOR_COPY): React.ReactNode {
  const pathname = usePathname() ?? "";
  const router = useRouter();
  const [pending, setPending] = useState<{ href: string; resolve: (ok: boolean) => void; onLeave?: () => void } | null>(null);
  const pendingRef = useRef(pending);
  pendingRef.current = pending;

  /** Opens the dialog for `href`; resolves true on "Leave without saving". */
  const ask = useCallback((href: string, onLeave?: () => void): Promise<boolean> => {
    pendingRef.current?.resolve(false);
    return new Promise<boolean>((resolve) => setPending({ href, resolve, onLeave }));
  }, []);

  useRegisterNavigationGuard(
    dirty ? (href) => (typeof window !== "undefined" && !leavesPage(href, pathname, window.location.origin) ? true : ask(href)) : null,
  );

  useEffect(() => {
    if (!dirty) return;
    const onBeforeUnload = (event: BeforeUnloadEvent) => {
      event.preventDefault();
      // Older browsers need returnValue set to show the prompt.
      event.returnValue = "";
    };
    // Capture phase on the document: runs before the link's own (React) click handler, which then
    // sees defaultPrevented and does not navigate.
    const onClick = (event: MouseEvent) => {
      const href = guardedHref(event, pathname, window.location.origin);
      if (!href) return;
      event.preventDefault();
      void ask(href, () => router.push(href));
    };
    window.addEventListener("beforeunload", onBeforeUnload);
    document.addEventListener("click", onClick, true);
    return () => {
      window.removeEventListener("beforeunload", onBeforeUnload);
      document.removeEventListener("click", onClick, true);
    };
  }, [ask, dirty, pathname, router]);

  // A guard that goes clean (saved) while asking lets the navigation through.
  useEffect(() => {
    if (!dirty && pendingRef.current) {
      pendingRef.current.resolve(true);
      setPending(null);
    }
  }, [dirty]);

  const cancel = useCallback(() => {
    pendingRef.current?.resolve(false);
    setPending(null);
  }, []);
  const leave = useCallback(() => {
    const current = pendingRef.current;
    setPending(null);
    // A held link navigates at once; a navigator awaiting the guard pushes when this resolves.
    current?.onLeave?.();
    current?.resolve(true);
  }, []);

  if (!pending) return null;
  return (
    <ConfirmDialog
      title={copy.title}
      description={copy.description}
      confirmLabel={copy.confirmLabel}
      cancelLabel="Stay"
      destructive
      onConfirm={leave}
      onCancel={cancel}
    />
  );
}
