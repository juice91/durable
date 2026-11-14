"use client";

import { useEffect } from "react";

/**
 * The titles pages ask for, oldest first. Only the newest is enforced, so two mounted headers never
 * fight over `document.title` (each would undo the other's write).
 */
const stack: { title: string }[] = [];
let observer: MutationObserver | null = null;

function apply() {
  const top = stack[stack.length - 1];
  if (top && document.title !== top.title) document.title = top.title;
}

/**
 * Keeps `document.title` at `title` while the calling page is mounted. Next re-applies the root
 * layout's static metadata title after client navigations and Back (the browser check found every
 * route showing the bare product name, and the editor losing the agent's name after Back from
 * Compare), so setting it once is not enough: a `<head>` observer puts it back whenever something
 * else overwrites it. Null leaves the title alone.
 */
export function useDocumentTitle(title: string | null): void {
  useEffect(() => {
    if (title === null || typeof document === "undefined") return;
    const entry = { title };
    stack.push(entry);
    apply();
    if (!observer && typeof MutationObserver !== "undefined") {
      observer = new MutationObserver(apply);
      observer.observe(document.head, { subtree: true, childList: true, characterData: true });
    }
    return () => {
      const index = stack.indexOf(entry);
      if (index !== -1) stack.splice(index, 1);
      if (stack.length === 0) {
        observer?.disconnect();
        observer = null;
      } else {
        apply();
      }
    };
  }, [title]);
}
