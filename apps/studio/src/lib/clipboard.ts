/**
 * Copies `text` to the clipboard. Resolves true when it was copied; false when the clipboard is not
 * available (an insecure context, an old browser, jsdom) or the write was refused. Never throws, so
 * callers only choose what to say.
 */
export async function copyText(text: string): Promise<boolean> {
  try {
    if (typeof navigator === "undefined" || !navigator.clipboard?.writeText) return false;
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    return false;
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
