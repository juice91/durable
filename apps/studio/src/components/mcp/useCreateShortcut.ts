"use client";

import { useEffect, useRef } from "react";
import { shouldHandleShortcut } from "@/components/frame/keyboard";

/**
 * `c` runs the page's primary create action (decision "Keyboard shortcuts"), with the frame's guard:
 * ignored while typing in a field, with a modifier held, or while a dialog or menu is open.
 * Local to the MCP pages until SL.5's shortcut engine offers a registration API.
 */
export function useCreateShortcut(onCreate: () => void, enabled = true): void {
  const callback = useRef(onCreate);
  callback.current = onCreate;
  useEffect(() => {
    if (!enabled) return;
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key !== "c" || event.shiftKey || !shouldHandleShortcut(event)) return;
      event.preventDefault();
      callback.current();
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [enabled]);
}
