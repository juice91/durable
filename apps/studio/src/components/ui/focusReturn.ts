"use client";

import { useCallback, useRef } from "react";

/**
 * Radix returns focus on close only to a `*.Trigger`. Studio opens overlays from rows, shortcuts and
 * URLs too, so this remembers whatever had focus when the overlay opened (Radix fires
 * `onOpenAutoFocus` before it moves focus) and puts focus back there on close, if that element is
 * still in the document. Without one, Radix's default applies.
 *
 * `target` exposes the remembered element so a stack can hand it on (see Sheet), and `skipReturn`
 * suppresses the return for a close the user did not ask for (a Sheet pushed out of the stack).
 */
export function useFocusReturn<OpenEvent extends Event, CloseEvent extends Event>(
  handlers: {
    onOpenAutoFocus?: (event: OpenEvent) => void;
    onCloseAutoFocus?: (event: CloseEvent) => void;
  } = {},
  skipReturn?: () => boolean,
) {
  const target = useRef<HTMLElement | null>(null);
  const { onOpenAutoFocus, onCloseAutoFocus } = handlers;

  const handleOpen = useCallback(
    (event: OpenEvent) => {
      const active = typeof document !== "undefined" ? document.activeElement : null;
      target.current = active instanceof HTMLElement && active !== document.body ? active : null;
      onOpenAutoFocus?.(event);
    },
    [onOpenAutoFocus],
  );

  const handleClose = useCallback(
    (event: CloseEvent) => {
      onCloseAutoFocus?.(event);
      if (event.defaultPrevented) return;
      const element = target.current;
      target.current = null;
      if (skipReturn?.()) {
        event.preventDefault();
        return;
      }
      if (element && element.isConnected) {
        event.preventDefault();
        element.focus();
      }
    },
    [onCloseAutoFocus, skipReturn],
  );

  return { onOpenAutoFocus: handleOpen, onCloseAutoFocus: handleClose, target };
}
