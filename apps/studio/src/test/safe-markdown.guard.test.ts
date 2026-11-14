/**
 * Model and user text can carry tenant content (uploaded knowledge), so every Markdown render in
 * Studio goes through components/markdown/SafeMarkdown.tsx: images as alt text only, skipHtml, the
 * safe URL transform, a fixed plugin list. This scans the source: outside SafeMarkdown.tsx,
 * `react-markdown` may only be imported for types (no runtime import, dynamic `import()` or
 * next/dynamic, `require`, or re-export), no file renders `<ReactMarkdown` or passes `remarkPlugins`
 * / `rehypePlugins`, and raw-HTML plugins are banned everywhere. It also checks SafeMarkdown's own
 * ReactMarkdown keeps the safe options and its plugin allowlist.
 */
import fs from "node:fs";
import path from "node:path";
import { describe, expect, it } from "vitest";

const SRC = path.resolve(__dirname, "..");
const SAFE = path.join(SRC, "components", "markdown", "SafeMarkdown.tsx");

function sourceFiles(): string[] {
  const out: string[] = [];
  const walk = (dir: string) => {
    for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
      const full = path.join(dir, entry.name);
      if (entry.isDirectory()) walk(full);
      else if (/\.(tsx?|jsx?)$/.test(entry.name) && !/\.test\.tsx?$/.test(entry.name)) out.push(full);
    }
  };
  walk(SRC);
  return out;
}

/** Problems with one file's Markdown use (empty when it is safe). Exported shape kept local. */
export function markdownViolations(file: string, source: string): string[] {
  const rel = path.relative(SRC, file);
  const problems: string[] = [];
  if (/rehype-raw|rehypeRaw|rehype-unwrap-images|allowDangerousHtml/.test(source)) problems.push(`${rel}: raw-HTML Markdown plugin`);
  if (file === SAFE) return problems;
  const imports = source.match(/import\s+[^;]*?from\s+["']react-markdown["']/g) ?? [];
  for (const imp of imports) {
    const typeOnly = /^import\s+type\s/.test(imp) || /^import\s*\{\s*(type\s+\w+\s*,?\s*)+\}\s*from/.test(imp);
    if (!typeOnly) problems.push(`${rel}: imports react-markdown at runtime; use SafeMarkdown`);
  }
  // A bare side-effect import, `import("react-markdown")` (next/dynamic included), `require(...)`.
  if (/import\s*["']react-markdown["']|import\s*\(\s*["'`]react-markdown["'`]\s*\)|require\s*\(\s*["'`]react-markdown["'`]\s*\)/.test(source)) {
    problems.push(`${rel}: loads react-markdown dynamically; use SafeMarkdown`);
  }
  const reexports = source.match(/export\s+[^;]*?from\s+["']react-markdown["']/g) ?? [];
  for (const exp of reexports) {
    if (!/^export\s+type\s/.test(exp)) problems.push(`${rel}: re-exports react-markdown; use SafeMarkdown`);
  }
  if (/<ReactMarkdown\b|<Markdown\b/.test(source)) problems.push(`${rel}: renders ReactMarkdown directly; use SafeMarkdown`);
  if (/\b(remarkPlugins|rehypePlugins)\s*[=:]/.test(source)) problems.push(`${rel}: passes Markdown plugins; SafeMarkdown's list is fixed`);
  return problems;
}

describe("safe Markdown guard", () => {
  it("no file renders react-markdown except SafeMarkdown.tsx", () => {
    const problems = sourceFiles().flatMap((f) => markdownViolations(f, fs.readFileSync(f, "utf8")));
    expect(problems).toEqual([]);
  });

  it("SafeMarkdown's ReactMarkdown keeps skipHtml, the safe img override and the URL transform", () => {
    const src = fs.readFileSync(SAFE, "utf8");
    const tag = /<ReactMarkdown\s+[a-z][\s\S]*?\n\s*>\n/.exec(src)?.[0] ?? "";
    expect(tag).toMatch(/\bskipHtml\b/);
    expect(tag).toMatch(/urlTransform=\{safeUrlTransform\}/);
    expect(tag).toMatch(/img:\s*safeMarkdownComponents\.img/);
    expect(src).toMatch(/img:\s*\(\{\s*alt\s*\}\)\s*=>\s*<SafeImage alt=\{alt\}\s*\/>/);
    // The plugin allowlist: GFM and the chat citations, nothing a caller passes in; no rehype plugins.
    expect(tag).toMatch(/remarkPlugins=\{plugins\}/);
    expect(tag).not.toMatch(/rehypePlugins/);
    expect(src).toMatch(/const plugins = React\.useMemo\(\(\) => \(citations \? \[remarkGfm, remarkCitations\(citations\)\] : \[remarkGfm\]\), \[citations\]\);/);
    expect(src.match(/remarkPlugins|rehypePlugins/g)).toEqual(["remarkPlugins"]);
  });

  it("flags a direct ReactMarkdown render and a runtime import, accepts a type-only import", () => {
    const f = path.join(SRC, "components", "x", "Bad.tsx");
    expect(markdownViolations(f, 'import ReactMarkdown from "react-markdown";\nconst x = <ReactMarkdown skipHtml>{t}</ReactMarkdown>;')).toHaveLength(2);
    expect(markdownViolations(f, 'import { type Components } from "react-markdown";')).toEqual([]);
    expect(markdownViolations(f, 'import type { Components } from "react-markdown";')).toEqual([]);
    expect(markdownViolations(f, 'import rehypeRaw from "rehype-raw";')).toHaveLength(1);
    expect(markdownViolations(f, 'const M = dynamic(() => import("react-markdown"));')).toHaveLength(1);
    expect(markdownViolations(f, "const M = await import('react-markdown');")).toHaveLength(1);
    expect(markdownViolations(f, 'const M = require("react-markdown");')).toHaveLength(1);
    expect(markdownViolations(f, 'import "react-markdown";')).toHaveLength(1);
    expect(markdownViolations(f, 'export { default } from "react-markdown";')).toHaveLength(1);
    expect(markdownViolations(f, 'export * from "react-markdown";')).toHaveLength(1);
    expect(markdownViolations(f, 'export type { Components } from "react-markdown";')).toEqual([]);
    expect(markdownViolations(f, "<SafeMarkdown remarkPlugins={[x]}>{t}</SafeMarkdown>")).toHaveLength(1);
    // rehype-raw is banned in SafeMarkdown.tsx too.
    expect(markdownViolations(SAFE, 'import rehypeRaw from "rehype-raw";')).toHaveLength(1);
  });
});
