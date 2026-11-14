/**
 * Page-level accessibility smoke checks (SL.15). A page test calls `expectAccessiblePage` with the
 * page element and its own fixtures already in place (its `next/navigation` and API mocks); the
 * helper renders it in the whole frame (`FrameHarness`) under the stored theme, waits until the page
 * is ready, then checks: the theme applied, exactly one level-one heading, and no axe violation with
 * the page rules on (one <main>, one h1). Overlays (Sheets, popovers) are checked by their own tests.
 *
 * Give the calling test `AXE_TIMEOUT_MS` (two themes, two axe passes).
 */
import React from "react";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { expect } from "vitest";
import { FrameHarness, mockMatchMedia } from "@/components/frame/frameTesting";
import { THEME_STORAGE_KEY, type ResolvedTheme } from "@/lib/theme";
import { expectNoAxeViolations, PAGE_RULES } from "./a11y";

export const THEMES: readonly ResolvedTheme[] = ["light", "dark"];

export interface AccessiblePageOptions {
  /** Resolves once the page has loaded its fixtures (default: the h1 and no "Loading" status left). */
  ready?: () => Promise<unknown>;
  /** Themes to check (default: light and dark). */
  themes?: readonly ResolvedTheme[];
}

async function defaultReady(): Promise<void> {
  await screen.findByRole("heading", { level: 1 });
  await waitFor(() => expect(screen.queryAllByText(/^Loading/)).toHaveLength(0));
}

/** Renders `page()` in the frame once per theme and asserts it is accessible (see the module comment). */
export async function expectAccessiblePage(page: () => React.ReactElement, options: AccessiblePageOptions = {}): Promise<void> {
  const { ready = defaultReady, themes = THEMES } = options;
  for (const theme of themes) {
    const media = mockMatchMedia(true);
    const root = document.documentElement;
    try {
      // The root layout sets lang="en" on <html>; jsdom's document has none.
      root.lang = "en";
      root.classList.remove("dark");
      window.localStorage.setItem(THEME_STORAGE_KEY, theme);
      render(<FrameHarness>{page()}</FrameHarness>);
      await ready();
      await waitFor(() => expect(root.classList.contains("dark"), `theme ${theme}`).toBe(theme === "dark"));
      const h1s = screen.getAllByRole("heading", { level: 1 });
      expect(h1s.map((h) => h.textContent), `one h1 in ${theme}`).toHaveLength(1);
      await expectNoAxeViolations(document, { rules: PAGE_RULES });
    } finally {
      cleanup();
      window.localStorage.removeItem(THEME_STORAGE_KEY);
      root.classList.remove("dark");
      media.restore();
    }
  }
}
