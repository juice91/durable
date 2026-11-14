"use client";

import React from "react";
import { Toaster as SonnerToaster, toast as sonnerToast } from "sonner";

/**
 * Show a toast. A declared export, not `export { toast }`: Next's "use client" transform does not
 * see a re-exported import binding, and webpack then warned "'toast' is not exported" on every page.
 */
export const toast: typeof sonnerToast = sonnerToast;

/**
 * Transient confirmations (copied, saved, restored), bottom-right. Styled with tokens, so it follows
 * the theme class on <html>. Additive only: an inline `role="status"` or `role="alert"` a page shows
 * stays where it is; a toast never replaces it.
 */
export function Toaster(props: React.ComponentProps<typeof SonnerToaster>) {
  return (
    <SonnerToaster
      position="bottom-right"
      closeButton
      toastOptions={{
        unstyled: true,
        classNames: {
          toast: "flex w-full items-start gap-2 rounded-lg border border-border bg-surface-raised p-3 text-label text-foreground shadow-overlay",
          title: "font-medium",
          description: "text-caption text-muted-foreground",
          actionButton: "rounded-md bg-primary px-2 py-1 text-caption font-medium text-primary-foreground",
          cancelButton: "rounded-md border border-border px-2 py-1 text-caption",
          closeButton: "rounded-sm border border-border bg-surface-raised text-muted-foreground hover:text-foreground",
          error: "text-destructive-text",
          success: "[&_[data-icon]]:text-success-text",
          warning: "[&_[data-icon]]:text-warning-text",
          info: "[&_[data-icon]]:text-info-text",
        },
      }}
      {...props}
    />
  );
}
