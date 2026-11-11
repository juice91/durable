import { readFileSync, readdirSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

const appDir = dirname(fileURLToPath(import.meta.url));

function pageFiles(directory: string): string[] {
  return readdirSync(directory, { withFileTypes: true }).flatMap((entry) => {
    const path = join(directory, entry.name);
    if (entry.isDirectory()) return pageFiles(path);
    return entry.name === "page.tsx" ? [path] : [];
  });
}

describe("Studio application shell", () => {
  it("loads the global stylesheet from the root layout", () => {
    const layout = readFileSync(join(appDir, "layout.tsx"), "utf8");

    expect(layout).toMatch(/import\s+["']\.\/globals\.css["']/);
  });

  it("uses the Tailwind 4 stylesheet entrypoint and theme tokens", () => {
    const css = readFileSync(join(appDir, "globals.css"), "utf8");

    expect(css).toContain('@import "tailwindcss";');
    expect(css).toContain("--color-background:");
    expect(css).toContain("--color-foreground:");
    expect(css).toContain("--color-border:");
    expect(css).not.toContain("@tailwind base");
  });

  it("owns the application Layout only in the root layout", () => {
    const pagesWithTheirOwnShell = pageFiles(appDir).filter((path) =>
      readFileSync(path, "utf8").includes("<Layout"),
    );

    expect(pagesWithTheirOwnShell).toEqual([]);
  });
});
