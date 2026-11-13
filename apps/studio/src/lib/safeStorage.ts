/**
 * localStorage that never throws. Storage can be missing (the server), blocked (privacy settings,
 * some private modes) or full (quota): every helper falls back instead of throwing. Studio code
 * reads and writes its preferences through these. The theme's inline head script (`themeScript()`
 * in lib/theme.ts) stays standalone: it runs before any bundle loads.
 */

/** `window.localStorage`, or null on the server or when accessing it throws. */
export function safeLocalStorage(): Storage | null {
  try {
    return typeof window === "undefined" ? null : window.localStorage;
  } catch {
    return null;
  }
}

/** The stored string, or null when absent, unavailable or throwing. */
export function readStorage(key: string): string | null {
  try {
    return safeLocalStorage()?.getItem(key) ?? null;
  } catch {
    return null;
  }
}

/** Stores `value`; false when storage is unavailable or throws (quota). */
export function writeStorage(key: string, value: string): boolean {
  try {
    const storage = safeLocalStorage();
    if (!storage) return false;
    storage.setItem(key, value);
    return true;
  } catch {
    return false;
  }
}

/** Removes `key`; false when storage is unavailable or throws. */
export function removeStorage(key: string): boolean {
  try {
    const storage = safeLocalStorage();
    if (!storage) return false;
    storage.removeItem(key);
    return true;
  } catch {
    return false;
  }
}
