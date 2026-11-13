import { describe, expect, it } from "vitest";
import fs from "fs";
import path from "path";

// Destructive TEXT uses --destructive-text, which must read at WCAG AA (>= 4.5:1) on the page
// background, cards and the destructive/10 and /15 alert tints, in both themes. --destructive itself
// is for fills paired with --destructive-foreground (>= 4.5:1, checked below; too dark as text in
// dark mode: ~2:1).

const srcDir = path.join(__dirname, "..");
const css = fs.readFileSync(path.join(srcDir, "app", "globals.css"), "utf-8");

function block(selector: string): string {
  const start = css.indexOf(`${selector} {`);
  if (start < 0) throw new Error(`no ${selector} block in globals.css`);
  return css.slice(start, css.indexOf("}", start));
}

function token(scope: string, name: string): [number, number, number] {
  const match = new RegExp(`--${name}:\\s*([\\d.]+)\\s+([\\d.]+)%\\s+([\\d.]+)%`).exec(scope);
  if (!match) throw new Error(`--${name} missing`);
  return [Number(match[1]), Number(match[2]), Number(match[3])];
}

function hslToRgb([h, s, l]: [number, number, number]): [number, number, number] {
  const sat = s / 100;
  const light = l / 100;
  const k = (n: number) => (n + h / 30) % 12;
  const a = sat * Math.min(light, 1 - light);
  const f = (n: number) => light - a * Math.max(-1, Math.min(k(n) - 3, Math.min(9 - k(n), 1)));
  return [f(0), f(8), f(4)];
}

function luminance(rgb: [number, number, number]): number {
  const [r, g, b] = rgb.map((v) => (v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4));
  return 0.2126 * r + 0.7152 * g + 0.0722 * b;
}

function contrast(a: [number, number, number], b: [number, number, number]): number {
  const [hi, lo] = [luminance(a), luminance(b)].sort((x, y) => y - x);
  return (hi + 0.05) / (lo + 0.05);
}

function over(fg: [number, number, number], bg: [number, number, number], alpha: number): [number, number, number] {
  return [0, 1, 2].map((i) => alpha * fg[i] + (1 - alpha) * bg[i]) as [number, number, number];
}

describe.each([
  ["light", ":root"],
  ["dark", ".dark"],
])("--destructive-text in the %s theme", (_theme, selector) => {
  const scope = block(selector);
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
