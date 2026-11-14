"use client";

import { useCallback, useEffect, useRef } from "react";

/** The "Version N published, restored from vM" notice that takes focus after a restore. */
export const RESTORE_NOTICE_SELECTOR = "[data-restore-notice]";
/** How long after a restore a closing Sheet may not hand focus back (it closes when the URL drops). */
export const RESTORE_FOCUS_WINDOW_MS = 3000;
/** How long after focusing the notice a fall to <body> is caught and undone, once. */
export const RESTORE_FOCUS_SAFETY_MS = 500;

function notice(): HTMLElement | null {
  return document.querySelector<HTMLElement>(RESTORE_NOTICE_SELECTOR);
}

function onBody(): boolean {
  const active = document.activeElement;
  return !active || active === document.body || !active.isConnected;
}

export interface RestoreFocus {
  /** Call when a restore from the compare Sheet succeeded: the focus window opens. */
  arm: () => void;
  /** Call after renders that may have (re)created the notice (the Sheet closed, the editor remounted). */
  settle: (sheetOpen: boolean) => void;
  /** The compare Sheet's `onCloseAutoFocus`: inside the window it never hands focus back (the
   * Restore button and the Sheet are gone) and focuses the notice instead. */
  onSheetCloseAutoFocus: (event: Event) => void;
}

/**
 * Focus after a restore from the compare Sheet (browser re-check: focus reached the notice, then the
 * closing Sheet's focus return dropped it on <body> 30 ms later). Inside a time window that opens on
 * `arm`, the Sheet's own focus return is always prevented and redirected to the notice, whatever
 * order the confirm, the URL change and the editor remount happen in. After the notice takes focus, a
 * fall to <body> within 500 ms is undone once.
 */
export function useRestoreFocus(): RestoreFocus {
  const armedUntil = useRef(0);
  const pending = useRef(false);
  const safety = useRef<{ timer: number; listener: () => void; element: HTMLElement } | null>(null);

  const stopSafety = useCallback(() => {
    const current = safety.current;
    if (!current) return;
    window.clearTimeout(current.timer);
    current.element.removeEventListener("focusout", current.listener);
    safety.current = null;
  }, []);

  const focusNotice = useCallback((): boolean => {
    const element = notice();
    if (!element) return false;
    element.focus();
    if (safety.current?.element === element) return true;
    stopSafety();
    // Once: if focus falls to <body> shortly after (a late focus return, a removed element), put it
    // back on the notice as it is then (it may have been recreated).
    let used = false;
    const recheck = () => {
      window.setTimeout(() => {
        if (used || !onBody()) return;
        used = true;
        notice()?.focus();
      }, 0);
    };
    const timer = window.setTimeout(() => {
      recheck();
      stopSafety();
    }, RESTORE_FOCUS_SAFETY_MS);
    element.addEventListener("focusout", recheck);
    safety.current = { timer, listener: recheck, element };
    return true;
  }, [stopSafety]);

  useEffect(() => stopSafety, [stopSafety]);

  const arm = useCallback(() => {
    armedUntil.current = Date.now() + RESTORE_FOCUS_WINDOW_MS;
    pending.current = true;
  }, []);

  const settle = useCallback(
    (sheetOpen: boolean) => {
      if (!pending.current || sheetOpen) return;
      if (focusNotice()) pending.current = false;
    },
    [focusNotice],
  );

  const onSheetCloseAutoFocus = useCallback(
    (event: Event) => {
      if (Date.now() > armedUntil.current) return;
      event.preventDefault();
      focusNotice();
    },
    [focusNotice],
  );

  return { arm, settle, onSheetCloseAutoFocus };
}
