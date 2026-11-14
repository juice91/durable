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
