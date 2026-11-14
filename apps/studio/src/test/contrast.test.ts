import { describe, expect, it } from "vitest";
import fs from "fs";
import path from "path";

// WCAG 2.2 AA for the Studio colour tokens (globals.css), in both themes:
// - text tokens (foreground, muted-foreground and every *-text) >= 4.5:1 on every surface and on
//   their own status 10% and 15% tints;
// - UI tokens (ring, border-strong, input) >= 3:1 against every surface, the canvas included;
// - text and border-strong also on the fill overlays (foreground at 4 / 7 / 10%: hover, active,
//   selected) over every surface and the canvas;
// - primary-foreground on primary and destructive-foreground on destructive >= 4.5:1;
// - chart series (chartTheme.css) >= 3:1 on the surfaces charts sit on.
// Destructive TEXT uses --destructive-text; --destructive itself is for fills paired with
// --destructive-foreground (too dark as text in dark mode).

type Rgb = [number, number, number];
type Hsl = [number, number, number];

const srcDir = path.join(__dirname, "..");
const css = fs.readFileSync(path.join(srcDir, "app", "globals.css"), "utf-8");
const chartCss = fs.readFileSync(path.join(srcDir, "components", "metrics", "chartTheme.css"), "utf-8");

function block(source: string, selector: string, file = "globals.css"): string {
  const start = source.indexOf(`${selector} {`);
  if (start < 0) throw new Error(`no ${selector} block in ${file}`);
  return source.slice(start, source.indexOf("}", start));
}

function token(scope: string, name: string): Hsl {
  const match = new RegExp(`(?<![\\w-])--${name}:\\s*([\\d.]+)\\s+([\\d.]+)%\\s+([\\d.]+)%`).exec(scope);
  if (!match) throw new Error(`--${name} missing`);
  return [Number(match[1]), Number(match[2]), Number(match[3])];
}

function hslToRgb([h, s, l]: Hsl): Rgb {
  const sat = s / 100;
  const light = l / 100;
  const k = (n: number) => (n + h / 30) % 12;
  const a = sat * Math.min(light, 1 - light);
  const f = (n: number) => light - a * Math.max(-1, Math.min(k(n) - 3, Math.min(9 - k(n), 1)));
  return [f(0), f(8), f(4)];
}

function luminance(rgb: Rgb): number {
  const [r, g, b] = rgb.map((v) => (v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4));
  return 0.2126 * r + 0.7152 * g + 0.0722 * b;
}

function contrast(a: Rgb, b: Rgb): number {
  const [hi, lo] = [luminance(a), luminance(b)].sort((x, y) => y - x);
  return (hi + 0.05) / (lo + 0.05);
}

function over(fg: Rgb, bg: Rgb, alpha: number): Rgb {
  return [0, 1, 2].map((i) => alpha * fg[i] + (1 - alpha) * bg[i]) as Rgb;
}

const THEMES = [
  ["light", ":root", ".eap-viz"],
  ["dark", ".dark", ".dark .eap-viz"],
] as const;

/** Surfaces text sits on. The canvas is the frame behind the main card: only UI tokens are checked there. */
const TEXT_SURFACES = ["background", "surface", "surface-raised", "muted"] as const;
const UI_SURFACES = ["canvas", ...TEXT_SURFACES] as const;
/** The fill overlays: the foreground at these alphas (globals.css `--color-fill-*`). */
const FILLS = [
  ["fill-hover", 0.04],
  ["fill-active", 0.07],
  ["fill-selected", 0.1],
] as const;
/** Each status text token and the fill its tints are made from. */
const STATUS_TEXT = [
  ["destructive-text", "destructive"],
  ["success-text", "success"],
  ["warning-text", "warning"],
  ["info-text", "info"],
] as const;

describe.each(THEMES)("--destructive-text in the %s theme", (_theme, selector) => {
  const scope = block(css, selector);
  const text = hslToRgb(token(scope, "destructive-text"));
  const destructive = hslToRgb(token(scope, "destructive"));
  const background = hslToRgb(token(scope, "background"));
  const card = hslToRgb(token(scope, "card"));

  it.each([
    ["background", background],
    ["card", card],
    ["destructive/10 tint", over(destructive, background, 0.1)],
    ["destructive/15 tint", over(destructive, background, 0.15)],
  ] as const)("is at least 4.5:1 on %s", (_name, bg) => {
    expect(contrast(text, bg)).toBeGreaterThanOrEqual(4.5);
  });

  it("--destructive-foreground on a solid --destructive fill (buttons, fatal badge) is at least 4.5:1", () => {
    expect(contrast(hslToRgb(token(scope, "destructive-foreground")), destructive)).toBeGreaterThanOrEqual(4.5);
  });
});

describe.each(THEMES)("colour tokens in the %s theme", (_theme, selector) => {
  const scope = block(css, selector);
  const rgb = (name: string) => hslToRgb(token(scope, name));

  it("keeps surfaces neutral (saturation 0)", () => {
    for (const name of [...UI_SURFACES, "border", "border-strong", "foreground", "muted-foreground"]) {
      expect(token(scope, name)[1], `--${name}`).toBe(0);
    }
  });

  it("keeps card on surface and popover on surface-raised", () => {
    expect(token(scope, "card")).toEqual(token(scope, "surface"));
    expect(token(scope, "popover")).toEqual(token(scope, "surface-raised"));
  });

  describe.each(TEXT_SURFACES)("on --%s", (surface) => {
    it.each(["foreground", "muted-foreground", ...STATUS_TEXT.map(([text]) => text)])(
      "--%s is at least 4.5:1",
      (text) => {
        expect(contrast(rgb(text), rgb(surface))).toBeGreaterThanOrEqual(4.5);
      },
    );

    it.each(STATUS_TEXT)("--%s is at least 4.5:1 on its own --%s 10% and 15% tints", (text, fill) => {
      expect(contrast(rgb(text), over(rgb(fill), rgb(surface), 0.1))).toBeGreaterThanOrEqual(4.5);
      expect(contrast(rgb(text), over(rgb(fill), rgb(surface), 0.15))).toBeGreaterThanOrEqual(4.5);
    });

    // Still used as text by existing components (spanStyles, EventTimeline, LogLevelBadge).
    it.each([
      ["success", "success"],
      ["warning", "warning"],
    ])("legacy text use of --%s is at least 4.5:1, also on its own tints", (text, fill) => {
      expect(contrast(rgb(text), rgb(surface))).toBeGreaterThanOrEqual(4.5);
      expect(contrast(rgb(text), over(rgb(fill), rgb(surface), 0.15))).toBeGreaterThanOrEqual(4.5);
    });
  });

  // SL.15: the sidebar's labels, group titles and version line sit directly on the canvas.
  it.each(["foreground", "muted-foreground", ...STATUS_TEXT.map(([text]) => text)])(
    "--%s is at least 4.5:1 on --canvas (the sidebar)",
    (text) => {
      expect(contrast(rgb(text), rgb("canvas"))).toBeGreaterThanOrEqual(4.5);
    },
  );

  it.each(UI_SURFACES)("--ring and --border-strong are at least 3:1 against --%s", (surface) => {
    expect(contrast(rgb("ring"), rgb(surface))).toBeGreaterThanOrEqual(3);
    expect(contrast(rgb("border-strong"), rgb(surface))).toBeGreaterThanOrEqual(3);
  });

  it.each(UI_SURFACES)("--input (form control boundary, WCAG 1.4.11) is at least 3:1 against --%s", (surface) => {
    expect(contrast(rgb("input"), rgb(surface))).toBeGreaterThanOrEqual(3);
  });

  describe.each(UI_SURFACES)("on the fill overlays over --%s", (surface) => {
    it.each(FILLS)("%s: text tokens are at least 4.5:1 and border-strong at least 3:1", (_fill, alpha) => {
      const bg = over(rgb("foreground"), rgb(surface), alpha);
      for (const text of ["foreground", "muted-foreground", ...STATUS_TEXT.map(([t]) => t)]) {
        expect(contrast(rgb(text), bg), `--${text}`).toBeGreaterThanOrEqual(4.5);
      }
      expect(contrast(rgb("border-strong"), bg), "--border-strong").toBeGreaterThanOrEqual(3);
    });
  });

  it("--primary-foreground on --primary is at least 4.5:1", () => {
    expect(contrast(rgb("primary-foreground"), rgb("primary"))).toBeGreaterThanOrEqual(4.5);
  });

  it("--primary is the inverse of the page (achromatic)", () => {
    expect(token(scope, "primary")[1]).toBe(0);
    expect(token(scope, "primary")).toEqual(token(scope, "foreground"));
  });

  it.each([
    ["card-foreground", "card"],
    ["popover-foreground", "popover"],
    ["secondary-foreground", "secondary"],
    ["accent-foreground", "accent"],
  ])("legacy pair --%s on --%s is at least 4.5:1", (text, bg) => {
    expect(contrast(rgb(text), rgb(bg))).toBeGreaterThanOrEqual(4.5);
  });
});

describe.each(THEMES)("chart tokens in the %s theme", (_theme, selector, vizSelector) => {
  const scope = block(css, selector);
  const viz = block(chartCss, vizSelector, "chartTheme.css");

  describe.each(["background", "surface", "surface-raised"] as const)("on --%s", (surface) => {
    it.each(["viz-series-1", "viz-series-2", "viz-critical"])("--%s is at least 3:1", (series) => {
      expect(contrast(hslToRgb(token(viz, series)), hslToRgb(token(scope, surface)))).toBeGreaterThanOrEqual(3);
    });
  });
});

describe("theme token coverage", () => {
  const theme = css.slice(css.indexOf("@theme inline {"), css.indexOf("}", css.indexOf("@theme inline {")));

  it.each([
    "canvas",
    "surface",
    "surface-raised",
    "fill-hover",
    "fill-active",
    "fill-selected",
    "border-strong",
    "success-text",
    "warning-text",
    "info",
    "info-text",
  ])("maps --color-%s for Tailwind", (name) => {
    expect(theme).toContain(`--color-${name}:`);
  });

  it.each(FILLS)("--color-%s is the foreground at alpha %s", (name, alpha) => {
    expect(theme).toContain(`--color-${name}: hsl(var(--foreground) / ${alpha});`);
  });

  it("defines every colour token in both themes", () => {
    const light = block(css, ":root");
    const dark = block(css, ".dark");
    const names = [...light.matchAll(/(?<![\w-])--([a-z-]+):\s*[\d.]+\s+[\d.]+%\s+[\d.]+%/g)].map((m) => m[1]);
    expect(names.length).toBeGreaterThan(20);
    for (const name of names) expect(() => token(dark, name), `--${name} in .dark`).not.toThrow();
  });
});

function sourceFiles(dir: string, out: string[] = []): string[] {
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) sourceFiles(full, out);
    else if (/\.(tsx?|jsx?)$/.test(entry.name) && !/\.test\./.test(entry.name)) out.push(full);
  }
  return out;
}

describe("destructive text classes", () => {
  const lines = sourceFiles(srcDir).flatMap((file) =>
    fs.readFileSync(file, "utf-8").split("\n").map((content, i) => `${path.relative(srcDir, file)}:${i + 1}: ${content.trim()}`),
  );

  it("never use --destructive as a text colour", () => {
    expect(lines.filter((l) => /text-destructive(?![-\w/])/.test(l))).toEqual([]);
  });

  it("use destructive-foreground only on a solid destructive fill", () => {
    expect(lines.filter((l) => l.includes("text-destructive-foreground") && !/bg-destructive(?![-\w/])/.test(l))).toEqual([]);
  });
});
