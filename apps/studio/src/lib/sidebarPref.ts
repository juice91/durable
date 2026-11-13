/**
 * The sidebar's collapsed state before paint. The head script (`themeScript()` in lib/theme.ts)
 * copies `localStorage["studio.sidebar"]` to `<html data-sidebar="collapsed">`, so the first paint
 * already has the rail (globals.css keys the initial width on it). The frame reads the same
 * attribute as its state: after hydration's first pass (which must match the expanded server
 * markup) it re-renders as the rail, while the CSS keeps the rail's shape, so nothing visibly flips.
 * Storage stays the persisted value.
 */
export const SIDEBAR_STORAGE_KEY = "studio.sidebar";
export const SIDEBAR_ATTRIBUTE = "data-sidebar";
export const SIDEBAR_COLLAPSED = "collapsed";

/** Whether <html> says the sidebar is collapsed (false on the server). */
export function sidebarAttributeCollapsed(root?: HTMLElement): boolean {
  const el = root ?? (typeof document === "undefined" ? undefined : document.documentElement);
  return el?.getAttribute(SIDEBAR_ATTRIBUTE) === SIDEBAR_COLLAPSED;
}

/** Set or clear `data-sidebar="collapsed"` on <html>. */
export function applySidebarAttribute(collapsed: boolean, root?: HTMLElement): void {
  const el = root ?? (typeof document === "undefined" ? undefined : document.documentElement);
  if (!el) return;
  if (collapsed) el.setAttribute(SIDEBAR_ATTRIBUTE, SIDEBAR_COLLAPSED);
  else el.removeAttribute(SIDEBAR_ATTRIBUTE);
}
