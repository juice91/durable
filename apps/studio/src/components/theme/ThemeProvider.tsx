"use client";

import React, { createContext, useCallback, useContext, useEffect, useMemo, useState, useSyncExternalStore } from "react";
import {
  applyTheme,
  DARK_MEDIA_QUERY,
  DEFAULT_THEME,
  getStoredTheme,
  isThemePreference,
  prefersDark,
  resolveTheme,
  storeTheme,
  THEME_STORAGE_KEY,
  type ResolvedTheme,
  type ThemePreference,
} from "@/lib/theme";

export interface ThemeContextValue {
  /** What the user chose: "system", "light" or "dark". */
  theme: ThemePreference;
  /** What is applied now: "light" or "dark". */
  resolvedTheme: ResolvedTheme;
  setTheme: (theme: ThemePreference) => void;
}

const ThemeContext = createContext<ThemeContextValue | null>(null);

// The `dark` class the head script set on <html> before paint. Read through useSyncExternalStore so
// hydration uses the server value (no mismatch) and React re-renders with the real class right
// after hydrating, before the stored preference is read: consumers never show the wrong theme.
const subscribeNothing = () => () => {};
const htmlIsDark = () => document.documentElement.classList.contains("dark");
const serverIsDark = () => false;

function mediaQuery(): MediaQueryList | null {
  try {
    return typeof window !== "undefined" && typeof window.matchMedia === "function"
      ? window.matchMedia(DARK_MEDIA_QUERY)
      : null;
  } catch {
    return null;
  }
}

/**
 * Holds the theme preference, applies it to <html>, follows the OS live while on System and
 * follows changes made in other tabs. The head script (`ThemeScript`) has already applied the same
 * theme before paint, so the first effect here changes nothing visible.
 */
export function ThemeProvider({ children }: { children: React.ReactNode }) {
  // The server cannot know the preference, so the first render uses the default and the mount
  // effect reads the real one. The DOM is already correct thanks to the head script.
  const [theme, setThemeState] = useState<ThemePreference>(DEFAULT_THEME);
  const [mediaDark, setMediaDark] = useState(false);
  // Nothing is applied until the stored preference has been read, so the default never
  // overwrites what the head script set (that would flash).
  const [ready, setReady] = useState(false);

  useEffect(() => {
    setThemeState(getStoredTheme());
    setMediaDark(prefersDark());
    setReady(true);
  }, []);

  useEffect(() => {
    const mql = mediaQuery();
    if (!mql) return;
    const onChange = (event: MediaQueryListEvent) => setMediaDark(event.matches);
    if (typeof mql.addEventListener === "function") {
      mql.addEventListener("change", onChange);
      return () => mql.removeEventListener("change", onChange);
    }
    mql.addListener?.(onChange);
    return () => mql.removeListener?.(onChange);
  }, []);

  useEffect(() => {
    const onStorage = (event: StorageEvent) => {
      if (event.key !== THEME_STORAGE_KEY) return;
      // Key removed in another tab: back to System. An invalid value is ignored.
      if (event.newValue === null) setThemeState(DEFAULT_THEME);
      else if (isThemePreference(event.newValue)) setThemeState(event.newValue);
    };
    window.addEventListener("storage", onStorage);
    return () => window.removeEventListener("storage", onStorage);
  }, []);

  const headScriptDark = useSyncExternalStore(subscribeNothing, htmlIsDark, serverIsDark);
  const resolvedTheme: ResolvedTheme = ready
    ? resolveTheme(theme, mediaDark)
    : headScriptDark
      ? "dark"
      : "light";

  useEffect(() => {
    if (ready) applyTheme(resolvedTheme);
  }, [ready, resolvedTheme]);

  const setTheme = useCallback((next: ThemePreference) => {
    storeTheme(next);
    setThemeState(next);
  }, []);

  const value = useMemo(() => ({ theme, resolvedTheme, setTheme }), [theme, resolvedTheme, setTheme]);

  return <ThemeContext.Provider value={value}>{children}</ThemeContext.Provider>;
}

export function useTheme(): ThemeContextValue {
  const value = useContext(ThemeContext);
  if (!value) throw new Error("useTheme must be used inside <ThemeProvider>");
  return value;
}
