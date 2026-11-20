/**
 * Copies `text` to the clipboard. Resolves true when it was copied; false when the clipboard is not
 * available (an insecure context, an old browser, jsdom) or the write was refused. Never throws, so
 * callers only choose what to say.
 */
export async function copyText(text: string): Promise<boolean> {
  try {
    if (typeof navigator !== "undefined" && navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(text);
      return true;
    }
  } catch {
    // fall through to the textarea fallback (a refused write, an insecure context)
  }
  return execCommandCopy(text);
}

/** The old path: a hidden textarea, selected, and `execCommand("copy")`. False when it is not there. */
function execCommandCopy(text: string): boolean {
  if (typeof document === "undefined" || typeof document.execCommand !== "function") return false;
  const area = document.createElement("textarea");
  area.value = text;
  area.setAttribute("readonly", "");
  area.setAttribute("aria-hidden", "true");
  area.style.position = "fixed";
  area.style.top = "0";
  area.style.left = "-9999px";
  area.style.opacity = "0";
  const previous = document.activeElement instanceof HTMLElement ? document.activeElement : null;
  document.body.appendChild(area);
  try {
    area.select();
    return document.execCommand("copy");
  } catch {
    return false;
  } finally {
    document.body.removeChild(area);
    previous?.focus?.();
  }
}

/**
 * Reads text from the clipboard. Resolves to the clipboard text when it was read; null when the
 * clipboard is unavailable (insecure context, old browser, jsdom) or the read was refused (the
 * user denied the permission). Never throws, so callers only choose what to say.
 */
export async function readText(): Promise<string | null> {
  try {
    if (typeof navigator === "undefined" || !navigator.clipboard?.readText) return null;
    return await navigator.clipboard.readText();
  } catch {
    return null;
  }
}
