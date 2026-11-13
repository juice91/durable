import { describe, it, expect } from "vitest";
import fs from "fs";
import path from "path";

const componentsDir = path.join(__dirname, "..", "components");
const appDir = path.join(__dirname, "..", "app");

// Files that are allowed to use hardcoded colors (playground components)
const playgroundFiles = new Set([
  "playground",
  "components/playground",
  "src/components/playground",
]);

// Common hardcoded colors to check for - focus on patterns that need theme tokens
const problematicPatterns = [
  "bg-white",
  "border-gray",
  "divide-gray",
  "text-gray-500",
  "text-gray-600",
  "text-gray-700",
  "text-gray-900",
  "text-gray-400",
  "text-gray-800",
  "hover:bg-gray",
  "focus:ring-blue",
];

function isPlaygroundFile(filePath: string): boolean {
  const relPath = path.relative(process.cwd(), filePath);
  return relPath.includes("playground") || relPath.includes("components/playground");
}

function findNonPlaygroundFiles(dir: string, files: string[] = []): string[] {
  const entries = fs.readdirSync(dir, { withFileTypes: true });
  for (const entry of entries) {
    const fullPath = path.join(dir, entry.name);
    if (entry.isDirectory()) {
      findNonPlaygroundFiles(fullPath, files);
    } else if (entry.isFile() && entry.name.endsWith(".tsx")) {
      files.push(fullPath);
    }
  }
  return files;
}

describe("Non-playground components", () => {
  it("should not use hardcoded color classes", () => {
    const allFiles = [...findNonPlaygroundFiles(componentsDir), ...findNonPlaygroundFiles(appDir)];

    const violations: Array<{ file: string; lines: Array<{ line: number; content: string }> }> = [];

    for (const file of allFiles) {
      if (isPlaygroundFile(file)) continue;

      const content = fs.readFileSync(file, "utf-8");
      const lines = content.split("\n");
      const fileViolations: Array<{ line: number; content: string }> = [];

      for (let i = 0; i < lines.length; i++) {
        const line = lines[i];
        for (const pattern of problematicPatterns) {
          if (line.includes(pattern)) {
            fileViolations.push({ line: i + 1, content: line.trim() });
          }
        }
      }

      if (fileViolations.length > 0) {
        violations.push({ file: path.relative(process.cwd(), file), lines: fileViolations });
      }
    }

    if (violations.length > 0) {
      console.log("\nHardcoded color violations found:");
      violations.forEach(({ file, lines }) => {
        console.log(`\n${file}:`);
        lines.forEach(({ line, content }) => {
          console.log(`  Line ${line}: ${content}`);
        });
      });
      expect(violations).toHaveLength(0);
    }
  });
});

// Status and level colours must come from theme tokens (destructive, success, warning, muted, ...),
// never Tailwind palette hues: these files used to hardcode them.
const TOKEN_ONLY_FILES = [
  "components/traces/spanStyles.ts",
  "components/logs/LogLevelBadge.tsx",
  "components/runs/EventTimeline.tsx",
];
const PALETTE_COLOR =
  /\b(?:bg|text|border|ring|divide|fill|stroke|from|via|to|outline|decoration|shadow|accent|caret|placeholder)-(?:slate|gray|zinc|neutral|stone|red|orange|amber|yellow|lime|green|emerald|teal|cyan|sky|blue|indigo|violet|purple|fuchsia|pink|rose)-\d{2,3}\b|\b(?:bg|text|border)-(?:white|black)\b|#[0-9a-fA-F]{3,8}\b/;

describe("Status and level styles", () => {
  it.each(TOKEN_ONLY_FILES)("%s uses theme tokens only", (relative) => {
    const lines = fs.readFileSync(path.join(componentsDir, "..", relative), "utf-8").split("\n");
    const violations = lines
      .map((content, index) => ({ line: index + 1, content: content.trim() }))
      .filter(({ content }) => PALETTE_COLOR.test(content));
    expect(violations).toEqual([]);
  });

  it("every source file under src/ uses theme tokens only (no palette hues, white/black or hex)", () => {
    const srcDir = path.join(componentsDir, "..");
    const files: string[] = [];
    const walk = (dir: string) => {
      for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
        const full = path.join(dir, entry.name);
        if (entry.isDirectory()) walk(full);
        else if (/\.(tsx?|jsx?)$/.test(entry.name) && !/\.test\./.test(entry.name)) files.push(full);
      }
    };
    walk(srcDir);
    const violations = files.flatMap((file) =>
      fs
        .readFileSync(file, "utf-8")
        .split("\n")
        .map((content, index) => `${path.relative(srcDir, file)}:${index + 1}: ${content.trim()}`)
        .filter((line) => PALETTE_COLOR.test(line.slice(line.indexOf(": ") + 2))),
    );
    expect(files.length).toBeGreaterThan(50);
    expect(violations).toEqual([]);
  });

  it("the token check catches palette colours", () => {
    expect(PALETTE_COLOR.test('className="mb-3 bg-green-50 border-green-200"')).toBe(true);
    expect(PALETTE_COLOR.test('className="hover:bg-green-700"')).toBe(true);
    expect(PALETTE_COLOR.test('"bg-amber-950 text-sky-300"')).toBe(true);
    expect(PALETTE_COLOR.test('"fixed inset-0 bg-black/50"')).toBe(true);
    expect(PALETTE_COLOR.test('ok: "bg-green-600 dark:bg-green-500"')).toBe(true);
    expect(PALETTE_COLOR.test('"dark:text-red-400"')).toBe(true);
    expect(PALETTE_COLOR.test('ok: "bg-success", error: "text-destructive-text", w: "bg-warning/15"')).toBe(false);
  });
});
