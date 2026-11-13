import { act, fireEvent, render, screen } from "@testing-library/react";
import tailwind from "@tailwindcss/postcss";
import fs from "fs";
import path from "path";
import postcss from "postcss";
import React from "react";
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import { themeScript, THEME_STORAGE_KEY } from "@/lib/theme";
import { ThemeProvider, useTheme } from "./ThemeProvider";

// SL.1 acceptance: the no-flash contract, live OS following, cross-tab sync.
// jsdom has no matchMedia, so each test installs one; the head script runs via new Function.

type Listener = (event: { matches: boolean }) => void;

function installMatchMedia(initialDark: boolean) {
  const listeners = new Set<Listener>();
  const mql = {
    matches: initialDark,
    media: "(prefers-color-scheme: dark)",
    addEventListener: (_t: string, fn: Listener) => listeners.add(fn),
    removeEventListener: (_t: string, fn: Listener) => listeners.delete(fn),
  };
  Object.defineProperty(window, "matchMedia", { configurable: true, writable: true, value: vi.fn(() => mql) });
  return {
    setDark(dark: boolean) {
      mql.matches = dark;
      act(() => listeners.forEach((fn) => fn({ matches: dark })));
    },
  };
}

function Probe() {
  const { theme, resolvedTheme, setTheme } = useTheme();
  return (
    <div>
      <span data-testid="pref">{theme}</span>
      <span data-testid="resolved">{resolvedTheme}</span>
      {(["system", "light", "dark"] as const).map((p) => (
        <button key={p} onClick={() => setTheme(p)}>
          {p}
        </button>
      ))}
    </div>
  );
}

const root = document.documentElement;
const runHeadScript = () => new Function(themeScript())();
const rootState = () => ({ dark: root.classList.contains("dark"), scheme: root.style.colorScheme });

beforeEach(() => {
  window.localStorage.clear();
  root.className = "";
  root.removeAttribute("style");
});

afterEach(() => {
  // @ts-expect-error -- jsdom has no matchMedia; tests add it per case
  delete window.matchMedia;
  vi.restoreAllMocks();
});

type Stored = "system" | "light" | "dark" | "garbage" | "absent" | "throws";
const STORED: Stored[] = ["system", "light", "dark", "garbage", "absent", "throws"];

function seedStorage(stored: Stored) {
  if (stored === "throws") {
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new Error("SecurityError");
    });
  } else if (stored !== "absent") {
    window.localStorage.setItem(THEME_STORAGE_KEY, stored);
  }
}

function expectedDark(stored: Stored, osDark: boolean) {
  return stored === "dark" || (stored !== "light" && osDark);
}

/** Every attribute change on <html> that actually changed the value. */
function watchRealChanges() {
  const observer = new MutationObserver(() => {});
  observer.observe(root, { attributes: true, attributeOldValue: true, attributeFilter: ["class", "style"] });
  return () => {
    const records = observer.takeRecords();
    observer.disconnect();
    return records.filter((r, i) => {
      const after = records.slice(i + 1).find((n) => n.attributeName === r.attributeName);
      return (after ? after.oldValue : root.getAttribute(r.attributeName!)) !== r.oldValue;
    });
  };
}

describe("no-flash contract", () => {
  describe.each(STORED)("stored %s", (stored) => {
    it.each([true, false])("OS dark=%s: head script, then a provider that changes nothing", (osDark) => {
      installMatchMedia(osDark);
      seedStorage(stored);
      runHeadScript();
      const dark = expectedDark(stored, osDark);
      expect(rootState()).toEqual({ dark, scheme: dark ? "dark" : "light" });

      const stop = watchRealChanges();
      render(
        <ThemeProvider>
          <Probe />
        </ThemeProvider>,
      );
      expect(stop()).toEqual([]);
      expect(rootState()).toEqual({ dark, scheme: dark ? "dark" : "light" });
      expect(screen.getByTestId("resolved").textContent).toBe(dark ? "dark" : "light");
    });
  });

  it("the head script replaces a stale class instead of accumulating", () => {
    installMatchMedia(false);
    root.classList.add("dark");
    runHeadScript();
    expect(rootState()).toEqual({ dark: false, scheme: "light" });
  });
});

function mount() {
  render(
    <ThemeProvider>
      <Probe />
    </ThemeProvider>,
  );
}
const resolved = () => screen.getByTestId("resolved").textContent;

describe("System mode", () => {
  it("follows a live OS change, ignores it on Light/Dark, and re-follows on return to System", () => {
    const mm = installMatchMedia(false);
    mount();
    expect(rootState().dark).toBe(false);
    mm.setDark(true);
    expect(rootState()).toEqual({ dark: true, scheme: "dark" });
    mm.setDark(false);
    expect(rootState()).toEqual({ dark: false, scheme: "light" });

    fireEvent.click(screen.getByRole("button", { name: "light" }));
    mm.setDark(true);
    expect(rootState()).toEqual({ dark: false, scheme: "light" });
    expect(resolved()).toBe("light");

    fireEvent.click(screen.getByRole("button", { name: "dark" }));
    mm.setDark(false);
    expect(rootState()).toEqual({ dark: true, scheme: "dark" });

    // The OS flipped while an explicit theme was chosen: System picks up the current value.
    fireEvent.click(screen.getByRole("button", { name: "system" }));
    expect(rootState()).toEqual({ dark: false, scheme: "light" });
    mm.setDark(true);
    expect(rootState()).toEqual({ dark: true, scheme: "dark" });
    expect(window.localStorage.getItem(THEME_STORAGE_KEY)).toBe("system");
  });
});

function otherTab(newValue: string | null, key: string | null = THEME_STORAGE_KEY) {
  act(() => {
    window.dispatchEvent(new StorageEvent("storage", { key, newValue }));
  });
}

describe("cross-tab sync", () => {
  it("applies a valid value written by another tab", () => {
    installMatchMedia(false);
    mount();
    otherTab("dark");
    expect(screen.getByTestId("pref").textContent).toBe("dark");
    expect(rootState()).toEqual({ dark: true, scheme: "dark" });
    otherTab("light");
    expect(rootState()).toEqual({ dark: false, scheme: "light" });
  });

  it("ignores an invalid value from another tab", () => {
    installMatchMedia(false);
    mount();
    fireEvent.click(screen.getByRole("button", { name: "dark" }));
    otherTab("purple");
    expect(screen.getByTestId("pref").textContent).toBe("dark");
    expect(rootState()).toEqual({ dark: true, scheme: "dark" });
  });

  it("ignores storage events for other keys", () => {
    installMatchMedia(false);
    mount();
    fireEvent.click(screen.getByRole("button", { name: "dark" }));
    otherTab("light", "something.else");
    expect(screen.getByTestId("pref").textContent).toBe("dark");
  });

  it("a key removed in another tab goes back to System", () => {
    installMatchMedia(true);
    mount();
    fireEvent.click(screen.getByRole("button", { name: "light" }));
    otherTab(null);
    expect(screen.getByTestId("pref").textContent).toBe("system");
    expect(rootState().dark).toBe(true);
  });
});

// ---- Tokens and fonts (static checks on globals.css, layout.tsx and the compiled CSS) ----

const srcDir = path.join(__dirname, "..", "..");
const cssPath = path.join(srcDir, "app", "globals.css");
const css = fs.readFileSync(cssPath, "utf-8");
const read = (...p: string[]) => fs.readFileSync(path.join(srcDir, ...p), "utf-8");

function block(selector: string): string {
  const start = css.indexOf(`${selector} {`);
  if (start < 0) throw new Error(`no ${selector} block`);
  return css.slice(start, css.indexOf("\n  }", start));
}
const HSL_TRIPLE = /^[\d.]+\s+[\d.]+%\s+[\d.]+%$/;
const declared = (scope: string) => [...scope.matchAll(/(?<![\w-])--([\w-]+):\s*([^;]+);/g)].map((m) => [m[1], m[2].trim()]);

describe("colour tokens", () => {
  const light = declared(block(":root"));
  const colourTokens = light.filter(([, v]) => HSL_TRIPLE.test(v)).map(([n]) => n);
  const darkNames = new Set(declared(block(".dark")).map(([n]) => n));

  it("defines the decision's tokens as HSL triples in :root", () => {
    for (const n of ["canvas", "surface", "surface-raised", "border-strong", "success-text", "warning-text", "info", "info-text"]) {
      expect(colourTokens, n).toContain(n);
    }
  });

  it("every colour token in :root has a .dark override", () => {
    expect(colourTokens.filter((n) => !darkNames.has(n))).toEqual([]);
  });

  it("every --color-* mapping references a token defined in both themes", () => {
    const mapped = [...css.matchAll(/--color-[\w-]+:\s*hsl\(var\(--([\w-]+)\)/g)].map((m) => m[1]);
    expect(mapped.length).toBeGreaterThan(20);
    expect(mapped.filter((n) => !colourTokens.includes(n) || !darkNames.has(n))).toEqual([]);
  });
});

const COLOUR_UTILITIES = [
  "canvas", "surface", "surface-raised", "fill-hover", "fill-active", "fill-selected", "border-strong",
  "success", "success-text", "warning", "warning-text", "info", "info-text",
];
const OTHER_CLASSES: Record<string, RegExp> = {
  "rounded-sm": /var\(--radius\)/, "rounded-md": /var\(--radius\)/, "rounded-lg": /var\(--radius\)/,
  "rounded-xl": /var\(--radius\)/, "text-badge": /var\(--text-badge\)/, "text-caption": /var\(--text-caption\)/,
  "text-label": /var\(--text-label\)/, "text-body": /var\(--text-body\)/, "text-title": /var\(--text-title\)/,
  "text-display": /var\(--text-display\)/, "h-header": /var\(--spacing-header\)/, "h-row": /var\(--spacing-row\)/,
  "min-h-target": /var\(--spacing-target\)/, "size-icon": /var\(--spacing-icon\)/, "shadow-raised": /var\(--elevation-raised\)/,
  "font-sans": /var\(--font-sans\)/, "font-mono": /var\(--font-mono\)/,
};

describe("compiled Tailwind classes", () => {
  let out = "";
  const rule = (cls: string) => new RegExp(`\\.${cls.replace(/[/:]/g, "\\\\$&")}\\s*\\{([^}]*)\\}`).exec(out)?.[1] ?? "";

  beforeAll(async () => {
    const colour = COLOUR_UTILITIES.flatMap((n) => [`bg-${n}`, `text-${n}`, `border-${n}`]);
    const classes = [...colour, ...Object.keys(OTHER_CLASSES)].join(" ");
    const result = await postcss([tailwind()]).process(`${css}\n@source inline("${classes}");`, { from: cssPath });
    out = result.css;
  });

  it.each(COLOUR_UTILITIES.flatMap((n) => [`bg-${n}`, `text-${n}`, `border-${n}`]))("%s is a CSS variable reference", (cls) => {
    const body = rule(cls);
    expect(body, `${cls} not generated`).not.toBe("");
    expect(body).toMatch(/hsl\(var\(--[\w-]+\)/);
    expect(body).not.toMatch(/#[0-9a-f]{3,8}\b|rgb\(|oklch\(|\d+ \d+% \d+%/i);
  });

  it.each(Object.entries(OTHER_CLASSES))("%s references its token", (cls, pattern) => {
    expect(rule(cls)).toMatch(pattern);
  });
});

describe("no hard-coded colours in the new files", () => {
  const PALETTE =
    /\b(?:bg|text|border|ring|divide|fill|stroke|from|via|to|outline|shadow)-(?:slate|gray|zinc|neutral|stone|red|orange|amber|yellow|lime|green|emerald|teal|cyan|sky|blue|indigo|violet|purple|pink|rose)-\d{2,3}\b|\b(?:bg|text|border)-(?:white|black)\b|#[0-9a-fA-F]{3,8}\b|\b(?:rgb|rgba|hsl|oklch)\(/;
  const files = [
    ...fs.readdirSync(path.join(srcDir, "components", "theme")).map((f) => path.join("components", "theme", f)),
    ...fs.readdirSync(path.join(srcDir, "components", "ui")).map((f) => path.join("components", "ui", f)),
    path.join("app", "layout.tsx"),
    path.join("lib", "theme.ts"),
  ].filter((f) => /\.tsx?$/.test(f) && !/\.test\./.test(f));

  it("covers the theme, ui and layout files", () => {
    expect(files.length).toBeGreaterThan(10);
    expect(files).toContain(path.join("components", "theme", "ThemeProvider.tsx"));
  });

  it.each(files)("%s uses tokens only", (file) => {
    const hits = read(file).split("\n").map((l, i) => `${i + 1}: ${l.trim()}`).filter((l) => PALETTE.test(l));
    expect(hits).toEqual([]);
  });
});

describe("fonts", () => {
  it("layout loads both fontsource variable fonts through next/font/local on <html>", () => {
    const layout = read("app", "layout.tsx");
    const fonts = read("app", "fonts.ts");
    expect(fonts).toContain('from "next/font/local"');
    expect(fonts).toContain("@fontsource-variable/inter/files/inter-latin-wght-normal.woff2");
    expect(fonts).toContain("@fontsource-variable/jetbrains-mono/files/jetbrains-mono-latin-wght-normal.woff2");
    expect(fonts).toContain('variable: "--font-inter"');
    expect(fonts).toContain('variable: "--font-jetbrains-mono"');
    expect(fonts).toContain('adjustFontFallback: "Arial"');
    expect(layout).toContain('from "./fonts"');
    expect(layout).toMatch(/<html[^>]*className=\{`\$\{inter\.variable\} \$\{jetbrainsMono\.variable\}`\}/);
    for (const file of [
      "@fontsource-variable/inter/files/inter-latin-wght-normal.woff2",
      "@fontsource-variable/jetbrains-mono/files/jetbrains-mono-latin-wght-normal.woff2",
    ]) {
      expect(fs.existsSync(path.join(__dirname, "..", "..", "..", "node_modules", file)), file).toBe(true);
    }
  });

  it("font-family tokens name the loaded fonts with fallbacks", () => {
    const sans = /--font-sans:\s*([^;]+);/.exec(css)?.[1] ?? "";
    const mono = /--font-mono:\s*([^;]+);/.exec(css)?.[1] ?? "";
    expect(sans).toMatch(/^var\(--font-inter, [^)]+\),.*(system-ui|ui-sans-serif).*sans-serif$/);
    expect(mono).toMatch(/^var\(--font-jetbrains-mono, [^)]+\),.*(ui-monospace|Menlo).*monospace$/);
  });

  it("html uses --font-sans and code uses --font-mono", () => {
    expect(css).toMatch(/html\s*\{[^}]*font-family:\s*var\(--font-sans\)/);
    expect(css).toMatch(/code,[^{]*pre\s*\{[^}]*font-family:\s*var\(--font-mono\)/);
  });
});
