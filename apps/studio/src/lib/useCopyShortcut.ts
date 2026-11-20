"use client";

import { useEffect } from "react";
import { copyText } from "@/lib/clipboard";

/**
 * ⌘C / Ctrl+C on a focused row that carries `data-copy-text` copies that text, so a keyboard user
 * has the path a mouse user has with the copy button. Only when nothing is selected (a selection
 * keeps the browser's own copy) and focus is not in a field.
 */
export function useCopyShortcut(): void {
  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key.toLowerCase() !== "c" || !(event.metaKey || event.ctrlKey) || event.altKey || event.shiftKey) return;
      const target = document.activeElement;
      if (!(target instanceof HTMLElement)) return;
      if (target.matches("input, textarea, select, [contenteditable='true']")) return;
      if (window.getSelection()?.toString()) return;
      const row = target.closest<HTMLElement>("[data-copy-text]");
      const text = row?.getAttribute("data-copy-text");
      if (!text) return;
      event.preventDefault();
      void copyText(text);
    };
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, []);
}
