import React from "react";
import { themeScript } from "@/lib/theme";

/**
 * The blocking theme script for <head>: applies the stored (or OS) theme before first paint.
 * Pair it with `<html suppressHydrationWarning>`, since it changes <html>'s class and style
 * before React hydrates.
 *
 * Content-Security-Policy: if a CSP is added, this inline script must be allowed explicitly, either
 * with a per-request nonce (`<script nonce=…>`) or with the sha256 hash of `themeScript()` in
 * `script-src`. Without that the browser blocks it and the theme flashes on load.
 */
export function ThemeScript() {
  return <script id="studio-theme-script" dangerouslySetInnerHTML={{ __html: themeScript() }} />;
}
