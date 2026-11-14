"use client";

import React, { useEffect, useRef, useState } from "react";
import * as DialogPrimitive from "@radix-ui/react-dialog";
import { cn } from "@/lib/cn";
import { Button } from "@/components/ui/Button";
import { overlayClass } from "@/components/ui/Dialog";

interface ConfirmDialogProps {
  title: string;
  description?: React.ReactNode;
  confirmLabel: string;
  cancelLabel?: string;
  /** The action is running: confirm and Cancel are disabled and Escape is ignored. */
  busy?: boolean;
  /** Why the last attempt failed; shown inside the dialog and announced. */
  error?: string | null;
  destructive?: boolean;
  /** Only the confirm button is disabled (say, while what it acts on is still being checked); Cancel and Escape still work. */
  confirmDisabled?: boolean;
  onConfirm: () => void;
  onCancel: () => void;
  /**
   * Where focus goes when the dialog closes and the element that opened it is gone (a menu item, or
   * the row of what was deleted). Defaults to the page's `<main>`; never left on <body>.
   */
  fallbackFocus?: () => HTMLElement | null;
  /**
   * Checked when the dialog closes: true sends focus to `fallbackFocus` even though the opener still
   * exists (after a restore the opener, a Sheet's "Restore vN", is about to disappear with the Sheet).
   */
  skipOpenerReturn?: () => boolean;
}

/** The page's main landmark (focusable: the skip link targets it), or null outside the frame. */
function mainLandmark(): HTMLElement | null {
  return document.querySelector<HTMLElement>("main[tabindex]") ?? document.querySelector<HTMLElement>("main");
}

// Open dialogs, bottom to top. While any is open, every other child of <body> is inert (including
// children added later, via a MutationObserver: portals, toasts and dev overlays mount there), and
// lower dialogs are inert under the top one. Only elements this module made inert are restored, and
// only when the last dialog closes. Radix adds aria-hidden and the focus trap on top of this.
const openDialogs: HTMLElement[] = [];
const madeInert = new Set<HTMLElement>();
let bodyObserver: MutationObserver | null = null;

function applyInert() {
  const top = openDialogs[openDialogs.length - 1];
  for (const child of Array.from(document.body.children)) {
    if (!(child instanceof HTMLElement)) continue;
    if (child === top) {
      if (madeInert.has(child)) {
        child.removeAttribute("inert");
        madeInert.delete(child);
      }
    } else if (!child.hasAttribute("inert")) {
      child.setAttribute("inert", "");
      madeInert.add(child);
    }
  }
}

function pushDialog(container: HTMLElement) {
  openDialogs.push(container);
  applyInert();
  if (!bodyObserver && typeof MutationObserver !== "undefined") {
    bodyObserver = new MutationObserver(() => {
      if (openDialogs.length > 0) applyInert();
    });
    bodyObserver.observe(document.body, { childList: true });
  }
}

function popDialog(container: HTMLElement) {
  const index = openDialogs.indexOf(container);
  if (index !== -1) openDialogs.splice(index, 1);
  madeInert.delete(container);
  if (openDialogs.length > 0) {
    applyInert();
    return;
  }
  bodyObserver?.disconnect();
  bodyObserver = null;
  madeInert.forEach((element) => element.removeAttribute("inert"));
  madeInert.clear();
}

/**
 * A modal confirmation dialog, mounted while it should show (callers render it conditionally).
 * Built on the Radix Dialog: it renders into its own host under <body>, the rest of the page is made
 * inert, focus starts on Cancel and is trapped inside, Escape cancels (unless busy), an outside click
 * does nothing, and focus returns to the element that opened it (when that element still exists).
 */
export function ConfirmDialog({ title, description, confirmLabel, cancelLabel = "Cancel", busy = false, error = null, destructive = false, confirmDisabled = false, onConfirm, onCancel, fallbackFocus, skipOpenerReturn }: ConfirmDialogProps) {
  const [container, setContainer] = useState<HTMLElement | null>(null);
  const contentRef = useRef<HTMLDivElement>(null);
  const cancelRef = useRef<HTMLButtonElement>(null);
  const returnFocusRef = useRef<HTMLElement | null>(null);
  const fallbackRef = useRef(fallbackFocus);
  fallbackRef.current = fallbackFocus;
  const skipOpenerRef = useRef(skipOpenerReturn);
  skipOpenerRef.current = skipOpenerReturn;

  useEffect(() => {
    // Runs before the dialog content mounts, so focus is still on the opener.
    const active = document.activeElement;
    returnFocusRef.current = active instanceof HTMLElement && active !== document.body ? active : null;
    const element = document.createElement("div");
    element.setAttribute("data-confirm-dialog-root", "");
    document.body.appendChild(element);
    setContainer(element);
    return () => element.remove();
  }, []);

  useEffect(() => {
    if (!container) return;
    pushDialog(container);
    return () => {
      popDialog(container);
      const previous = returnFocusRef.current;
      if (previous && previous.isConnected && !skipOpenerRef.current?.()) {
        previous.focus();
        return;
      }
      // The opener is gone (the action removed it): focus a sensible element, never <body>.
      const fallback = fallbackRef.current?.() ?? mainLandmark();
      fallback?.focus({ preventScroll: true });
    };
  }, [container]);

  // Disabling the focused Cancel button would drop focus to <body>, outside the trap.
  useEffect(() => {
    if (!busy || !contentRef.current) return;
    const active = document.activeElement;
    if (!(active instanceof HTMLElement) || !contentRef.current.contains(active) || active.matches(":disabled")) {
      contentRef.current.focus();
    }
  }, [busy, container]);

  if (!container) return null;
  return (
    <DialogPrimitive.Root
      open
      onOpenChange={(open) => {
        if (!open && !busy) onCancel();
      }}
    >
      <DialogPrimitive.Portal container={container}>
        <DialogPrimitive.Overlay className={overlayClass} />
        <DialogPrimitive.Content
          ref={contentRef}
          aria-modal="true"
          aria-busy={busy}
          aria-describedby={undefined}
          onOpenAutoFocus={(event) => {
            event.preventDefault();
            cancelRef.current?.focus();
          }}
          // Focus return is done synchronously on unmount above; Radix's own runs a tick later.
          onCloseAutoFocus={(event) => event.preventDefault()}
          onEscapeKeyDown={(event) => {
            if (busy) event.preventDefault();
          }}
          onPointerDownOutside={(event) => event.preventDefault()}
          onInteractOutside={(event) => event.preventDefault()}
          className="fixed inset-x-4 top-1/2 z-50 mx-auto max-w-md -translate-y-1/2 space-y-4 rounded-xl border border-border bg-surface-raised p-5 text-foreground shadow-overlay focus:outline-none"
        >
          <DialogPrimitive.Title className="text-title font-semibold text-foreground">{title}</DialogPrimitive.Title>
          {description ? (
            <DialogPrimitive.Description asChild>
              <div className="text-body text-muted-foreground">{description}</div>
            </DialogPrimitive.Description>
          ) : null}
          {error ? <p role="alert" data-testid="confirm-dialog-error" className="rounded-md border border-destructive/30 bg-destructive/10 p-3 text-body text-destructive-text">{error}</p> : null}
          <div className="flex flex-wrap justify-end gap-2">
            <Button ref={cancelRef} variant="secondary" disabled={busy} onClick={onCancel}>{cancelLabel}</Button>
            <Button variant={destructive ? "destructive" : "primary"} disabled={busy || confirmDisabled} onClick={onConfirm} className={cn(busy && "cursor-not-allowed")}>
              {confirmLabel}
            </Button>
          </div>
        </DialogPrimitive.Content>
      </DialogPrimitive.Portal>
    </DialogPrimitive.Root>
  );
}
