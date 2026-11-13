"use client";

import React, { useEffect, useId, useRef, useState } from "react";
import { createPortal } from "react-dom";

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
  onConfirm: () => void;
  onCancel: () => void;
}

// Open dialogs, bottom to top. While any is open, every other child of <body> is inert (including
// children added later, via a MutationObserver: portals, toasts and dev overlays mount there), and
// lower dialogs are inert under the top one. Only elements this module made inert are restored, and
// only when the last dialog closes.
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

const FOCUSABLE = 'button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

/**
 * A modal confirmation dialog: rendered into <body>, the rest of the page is made inert while it is
 * open, focus starts on Cancel and is trapped inside, Escape cancels, and focus returns to the
 * element that opened it (when that element still exists).
 */
export function ConfirmDialog({ title, description, confirmLabel, cancelLabel = "Cancel", busy = false, error = null, destructive = false, onConfirm, onCancel }: ConfirmDialogProps) {
  const [container, setContainer] = useState<HTMLElement | null>(null);
  const dialogRef = useRef<HTMLDivElement>(null);
  const cancelRef = useRef<HTMLButtonElement>(null);
  const titleId = useId();
  const descriptionId = useId();

  useEffect(() => {
    const element = document.createElement("div");
    element.setAttribute("data-confirm-dialog-root", "");
    document.body.appendChild(element);
    setContainer(element);
    return () => element.remove();
  }, []);

  useEffect(() => {
    if (!container) return;
    const previous = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    pushDialog(container);
    cancelRef.current?.focus();
    return () => {
      popDialog(container);
      if (previous && previous.isConnected) previous.focus();
    };
  }, [container]);

  // Disabling the focused Cancel button would drop focus to <body>, outside the trap.
  useEffect(() => {
    if (!busy || !dialogRef.current) return;
    const active = document.activeElement;
    if (!(active instanceof HTMLElement) || !dialogRef.current.contains(active) || active.matches(":disabled")) {
      dialogRef.current.focus();
    }
  }, [busy, container]);

  const onKeyDown = (event: React.KeyboardEvent<HTMLDivElement>) => {
    if (event.key === "Escape") {
      event.preventDefault();
      event.stopPropagation();
      if (!busy) onCancel();
      return;
    }
    if (event.key !== "Tab" || !dialogRef.current) return;
    const focusable = Array.from(dialogRef.current.querySelectorAll<HTMLElement>(FOCUSABLE));
    if (focusable.length === 0) {
      event.preventDefault();
      return;
    }
    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    const active = document.activeElement;
    if (event.shiftKey && (active === first || !dialogRef.current.contains(active))) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && (active === last || !dialogRef.current.contains(active))) {
      event.preventDefault();
      first.focus();
    }
  };

  if (!container) return null;
  return createPortal(
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-background/80 p-4">
      <div
        ref={dialogRef}
        role="dialog"
        aria-modal="true"
        aria-busy={busy}
        tabIndex={-1}
        aria-labelledby={titleId}
        aria-describedby={description ? descriptionId : undefined}
        onKeyDown={onKeyDown}
        className="w-full max-w-md space-y-4 rounded-lg border border-border bg-card p-5 shadow-lg focus:outline-none"
      >
        <h2 id={titleId} className="text-lg font-semibold text-card-foreground">{title}</h2>
        {description ? <div id={descriptionId} className="text-sm text-muted-foreground">{description}</div> : null}
        {error ? <p role="alert" data-testid="confirm-dialog-error" className="rounded-md border border-destructive/30 bg-destructive/10 p-3 text-sm text-destructive">{error}</p> : null}
        <div className="flex justify-end gap-2">
          <button ref={cancelRef} type="button" disabled={busy} onClick={onCancel} className="rounded-md border border-border px-3 py-1.5 text-sm text-foreground hover:bg-accent disabled:cursor-not-allowed disabled:opacity-50">{cancelLabel}</button>
          <button
            type="button"
            disabled={busy}
            onClick={onConfirm}
            className={`rounded-md px-3 py-1.5 text-sm font-medium disabled:opacity-50 ${destructive ? "bg-destructive text-destructive-foreground" : "bg-primary text-primary-foreground"}`}
          >
            {confirmLabel}
          </button>
        </div>
      </div>
    </div>,
    container,
  );
}
