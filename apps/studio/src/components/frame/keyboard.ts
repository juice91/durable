/**
 * Guards for single-key shortcuts (`[` today; SL.5 adds the rest). A shortcut is ignored while the
 * user types in a field, while a modifier is held, and while a modal dialog is open.
 */
export function isTypingTarget(target: EventTarget | null): boolean {
  if (!target || typeof (target as Element).closest !== "function") return false;
  const element = target as HTMLElement;
  if (element.isContentEditable) return true;
  return element.closest('input, textarea, select, [contenteditable=""], [contenteditable="true"], [role="textbox"], [role="combobox"]') !== null;
}

/**
 * True while a dialog, drawer, popover, alert dialog or dropdown menu is open. Radix renders an
 * overlay's content only while it is open and does not set `aria-modal`, so presence of the role is
 * the test. An open menu counts: its keys belong to it (typeahead), not to page shortcuts.
 */
export function isModalOpen(doc: Document = document): boolean {
  return doc.querySelector('[role="dialog"], [role="alertdialog"], [role="menu"]') !== null;
}

/** True when a plain key press (no Ctrl, Meta or Alt) should run a global shortcut. */
export function shouldHandleShortcut(event: KeyboardEvent): boolean {
  if (event.defaultPrevented || event.ctrlKey || event.metaKey || event.altKey || event.isComposing) return false;
  if (isTypingTarget(event.target)) return false;
  return !isModalOpen(event.target instanceof Node && event.target.ownerDocument ? event.target.ownerDocument : document);
}
