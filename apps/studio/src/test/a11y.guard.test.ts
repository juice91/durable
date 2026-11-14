/**
 * Every test that runs axe gives itself `AXE_TIMEOUT_MS` (test/a11y.ts): a full axe pass over a page
 * takes seconds in jsdom, and tests at vitest's 5 s default timed out under machine load. This scans
 * the test files: for each axe call, the enclosing `it(`/`test(` must close with `}, AXE_TIMEOUT_MS);`.
 */
import fs from "node:fs";
import path from "node:path";
import { describe, expect, it } from "vitest";

const SRC = path.resolve(__dirname, "..");
const AXE_CALL = /\b(expectNoAxeViolations|runAxe|expectAccessiblePage)\(/;
const TEST_OPEN = /^(\s*)(it|test)(\.each\([\s\S]*?\))?(\.\w+)?\(/;

function testFiles(): string[] {
  const out: string[] = [];
  const walk = (dir: string) => {
    for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
      const full = path.join(dir, entry.name);
      if (entry.isDirectory()) walk(full);
      else if (/\.test\.tsx?$/.test(entry.name) && full !== __filename) out.push(full);
    }
  };
  walk(SRC);
  return out;
}

/** `file:line` of each axe call whose enclosing test does not pass AXE_TIMEOUT_MS. */
function missingAxeTimeouts(name: string, source: string): string[] {
  const lines = source.split("\n");
  const missing: string[] = [];
  lines.forEach((line, index) => {
    if (!AXE_CALL.test(line) || /^\s*(import|export|\*|\/\/)/.test(line) || /function\s+\w+\(/.test(line)) return;
    let indent: string | null = null;
    for (let j = index; j >= 0; j--) {
      const m = TEST_OPEN.exec(lines[j]);
      if (m) {
        indent = m[1];
        break;
      }
    }
    if (indent === null) return; // a helper outside any test (its callers are checked instead)
    for (let k = index; k < lines.length; k++) {
      if (lines[k].startsWith(`${indent}}, AXE_TIMEOUT_MS)`)) return;
      if (lines[k] === `${indent}});` || new RegExp(`^${indent}\\}, [\\w_]+\\);$`).test(lines[k])) break;
    }
    missing.push(`${name}:${index + 1}`);
  });
  return missing;
}

describe("axe tests carry AXE_TIMEOUT_MS", () => {
  it("every axe call sits in a test with the shared timeout", () => {
    const files = testFiles();
    const missing = files.flatMap((file) => missingAxeTimeouts(path.relative(SRC, file), fs.readFileSync(file, "utf8")));
    expect(files.length).toBeGreaterThan(50);
    expect(missing).toEqual([]);
  });

  it("catches a test without it, and passes one with it", () => {
    const bad = ['it("x", async () => {', "  await expectNoAxeViolations(document.body);", "});"].join("\n");
    const good = ['it("x", async () => {', "  await expectNoAxeViolations(document.body);", "}, AXE_TIMEOUT_MS);"].join("\n");
    expect(missingAxeTimeouts("f", bad)).toEqual(["f:2"]);
    expect(missingAxeTimeouts("f", good)).toEqual([]);
  });
});

describe("globals.css: prefers-reduced-motion", () => {
  const css = fs.readFileSync(path.join(SRC, "app", "globals.css"), "utf8");
  const block = /@media \(prefers-reduced-motion: reduce\)\s*\{([\s\S]*?)\n\}/.exec(css)?.[1] ?? "";

  it("has one global rule that stops transitions, animations and smooth scrolling for every element", () => {
    expect(css.match(/@media \(prefers-reduced-motion: reduce\)/g)).toHaveLength(1);
    expect(block).toMatch(/\*,\s*\*::before,\s*\*::after\s*\{/);
    for (const decl of [
      /animation-duration:\s*0\.01ms !important/,
      /animation-iteration-count:\s*1 !important/,
      /transition-duration:\s*0\.01ms !important/,
      /scroll-behavior:\s*auto !important/,
    ]) expect(block).toMatch(decl);
  });

  it("no script asks for smooth scrolling (it would ignore the CSS rule)", () => {
    const offenders: string[] = [];
    const walk = (dir: string) => {
      for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
        const full = path.join(dir, entry.name);
        if (entry.isDirectory()) walk(full);
        else if (/\.tsx?$/.test(entry.name) && !/\.test\.tsx?$/.test(entry.name) && /behavior:\s*["']smooth["']/.test(fs.readFileSync(full, "utf8"))) offenders.push(path.relative(SRC, full));
      }
    };
    for (const root of ["app", "components", "lib"]) walk(path.join(SRC, root));
    expect(offenders).toEqual([]);
  });
});
