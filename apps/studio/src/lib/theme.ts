import { SIDEBAR_ATTRIBUTE, SIDEBAR_COLLAPSED, SIDEBAR_STORAGE_KEY } from "./sidebarPref";
import { safeLocalStorage } from "@/lib/safeStorage";

/**
 * Studio theme: System / Light / Dark, default System.
 *
 * The preference is stored in `localStorage["studio.theme"]`. The resolved theme is applied to
 * <html> as the `dark` class plus an inline `color-scheme`, first by the blocking head script
 * (`themeScript()`, so there is no flash on load) and then kept in sync by `ThemeProvider`.
 * Every storage access is wrapped in try/catch: a blocked or throwing storage means System.
 */

export type ThemePreference = "system" | "light" | "dark";
export type ResolvedTheme = "light" | "dark";

export const THEME_STORAGE_KEY = "studio.theme";
export const THEME_PREFERENCES: readonly ThemePreference[] = ["system", "light", "dark"];
export const DEFAULT_THEME: ThemePreference = "system";
export const DARK_MEDIA_QUERY = "(prefers-color-scheme: dark)";

export function isThemePreference(value: unknown): value is ThemePreference {
  return value === "system" || value === "light" || value === "dark";
}

/** The stored preference, or "system" when nothing valid is stored or storage throws. */
export function getStoredTheme(storage: Storage | null = safeLocalStorage()): ThemePreference {
  try {
    const value = storage?.getItem(THEME_STORAGE_KEY);
    return isThemePreference(value) ? value : DEFAULT_THEME;
  } catch {
    return DEFAULT_THEME;
  }
}

/** Store the preference. Returns false when storage is unavailable or throws. */
export function storeTheme(pref: ThemePreference, storage: Storage | null = safeLocalStorage()): boolean {
  try {
    if (!storage) return false;
    storage.setItem(THEME_STORAGE_KEY, pref);
    return true;
  } catch {
    return false;
  }
}

export function resolveTheme(pref: ThemePreference, mediaDark: boolean): ResolvedTheme {
  if (pref === "system") return mediaDark ? "dark" : "light";
  return pref;
}

/** Whether the OS asks for dark; false where matchMedia is unavailable (SSR, old jsdom). */
export function prefersDark(): boolean {
  try {
    return typeof window !== "undefined" && typeof window.matchMedia === "function"
      ? window.matchMedia(DARK_MEDIA_QUERY).matches
      : false;
  } catch {
    return false;
  }
}

/** Set the `dark` class and `color-scheme` on <html> (or the given root). */
export function applyTheme(resolved: ResolvedTheme, root?: HTMLElement): void {
  const el = root ?? (typeof document === "undefined" ? undefined : document.documentElement);
  if (!el) return;
  el.classList.toggle("dark", resolved === "dark");
  el.style.colorScheme = resolved;
}

/** Store the preference and apply it now. Returns the resolved theme. */
export function setTheme(pref: ThemePreference, storage: Storage | null = safeLocalStorage()): ResolvedTheme {
  storeTheme(pref, storage);
  const resolved = resolveTheme(pref, prefersDark());
  applyTheme(resolved);
  return resolved;
}

/**
 * The blocking inline script for <head>. It must stay self-contained (no imports, ES5) because it
 * runs before any bundle: same key, same fallback to System, same class and color-scheme as above.
 * It also mirrors the stored sidebar state to `<html data-sidebar>` (lib/sidebarPref.ts).
 */
export function themeScript(): string {
  const key = JSON.stringify(THEME_STORAGE_KEY);
  const query = JSON.stringify(DARK_MEDIA_QUERY);
  const sidebarKey = JSON.stringify(SIDEBAR_STORAGE_KEY);
  const sidebarAttr = JSON.stringify(SIDEBAR_ATTRIBUTE);
  const collapsed = JSON.stringify(SIDEBAR_COLLAPSED);
  return (
    "(function(){try{" +
    "var p=null;try{p=window.localStorage.getItem(" + key + ")}catch(e){}" +
    'if(p!=="light"&&p!=="dark")p="system";' +
    'var d=p==="dark"||(p==="system"&&typeof window.matchMedia==="function"&&window.matchMedia(' + query + ").matches);" +
    "var r=document.documentElement;" +
    'if(d)r.classList.add("dark");else r.classList.remove("dark");' +
    'r.style.colorScheme=d?"dark":"light";' +
    "}catch(e){}" +
    // The sidebar rail (lib/sidebarPref.ts), on its own so a theme failure never blocks it.
    "try{var s=null;try{s=window.localStorage.getItem(" + sidebarKey + ")}catch(e){}" +
    "var h=document.documentElement;" +
    "if(s===" + collapsed + ")h.setAttribute(" + sidebarAttr + "," + collapsed + ");else h.removeAttribute(" + sidebarAttr + ");" +
    "}catch(e){}})();"
  );
}
