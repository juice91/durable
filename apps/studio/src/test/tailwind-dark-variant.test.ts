// @vitest-environment node
import fs from "fs";
import path from "path";
import postcss from "postcss";
import tailwind from "@tailwindcss/postcss";
import { describe, expect, it } from "vitest";

// Tailwind 4 compiles `dark:` under `@media (prefers-color-scheme: dark)` by default. Studio's theme
// is the .dark class on <html> (an explicit Light or Dark can differ from the OS), so globals.css
// must redefine the variant. This compiles the real stylesheet and checks the output.

const studioDir = path.join(__dirname, "..", "..");
const cssPath = path.join(studioDir, "src", "app", "globals.css");

async function compile(classes: string): Promise<string> {
  const css = `${fs.readFileSync(cssPath, "utf-8")}\n@source inline("${classes}");\n`;
  const result = await postcss([tailwind({ base: studioDir })]).process(css, { from: cssPath });
  return result.css;
}

function rule(css: string, selector: string): string {
  const start = css.indexOf(selector);
  if (start < 0) throw new Error(`${selector} not in compiled CSS`);
  return css.slice(start, css.indexOf("}", start) + 1);
}

describe("the dark: variant", () => {
  it("compiles dark:bg-surface under the .dark class, not a media query", async () => {
    const css = await compile("dark:bg-surface");
    const darkRule = rule(css, ".dark\\:bg-surface");
    expect(darkRule).toMatch(/:where\(\.dark, \.dark \*\)/);
    expect(darkRule).toContain("background-color: hsl(var(--surface))");
    expect(css).not.toMatch(/prefers-color-scheme:\s*dark\)\s*\{\s*\.dark\\:bg-surface/);
    expect(css).not.toContain("prefers-color-scheme");
  }, 20000);
});
