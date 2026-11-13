import localFont from "next/font/local";

// Self-hosted variable fonts (SIL OFL) from the fontsource packages, loaded through next/font/local:
// no network at build or run time, the files are preloaded, and next/font generates a
// metric-matched fallback face so the swap does not shift the layout. Latin only (the UI is
// English); other scripts fall back to the system stack in the font tokens. Weights 400-600 come
// from the one variable file.

export const inter = localFont({
  src: "../../node_modules/@fontsource-variable/inter/files/inter-latin-wght-normal.woff2",
  weight: "100 900",
  style: "normal",
  display: "swap",
  preload: true,
  variable: "--font-inter",
  adjustFontFallback: "Arial",
  fallback: ["ui-sans-serif", "system-ui", "-apple-system", "Segoe UI", "sans-serif"],
});

export const jetbrainsMono = localFont({
  src: "../../node_modules/@fontsource-variable/jetbrains-mono/files/jetbrains-mono-latin-wght-normal.woff2",
  weight: "100 800",
  style: "normal",
  display: "swap",
  preload: true,
  variable: "--font-jetbrains-mono",
  // No metric fallback: next/font only offers Arial or Times New Roman, neither monospace.
  adjustFontFallback: false,
  fallback: ["ui-monospace", "SFMono-Regular", "Menlo", "Consolas", "monospace"],
});
