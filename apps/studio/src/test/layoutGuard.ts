/**
 * Static checks for the phone-width layout rules (plan decision "Phone width and layout rules"):
 *
 * 1. Flex and grid children that hold text or tables get `min-w-0`.
 * 2. Wide tables scroll inside their own `overflow-x-auto` wrapper, never the page.
 * 3. KPI and card grids use `repeat(auto-fit, minmax(min(100%, 11rem), 1fr))`.
 * 4. No fixed width over 360 px without a `md:` or `lg:` prefix. (HARD)
 * 5. Side content never takes a column below 1024 px: it becomes a Sheet.
 * 6. Charts sit in `min-w-0` containers and size to them.
 * 7. Popovers become Sheets below 640 px.
 * Plus: no `w-screen` / `100vw` (or `100dvw`, `100svw`, `100lvw`) anywhere. (HARD)
 *
 * Each check is a pure function of one file's text, so `layout-guard.test.ts` runs them over
 * `src/app` and `src/components` and over fixture snippets that must be caught. What a static scan
 * can see is approximate by nature; each rule says what it looks at. Rules 4 and "screen" never
 * take an allowlist entry.
 */

export type LayoutRule = "R1" | "R2" | "R3" | "R4" | "R5" | "R6" | "R7" | "screen";

export interface Violation {
  rule: LayoutRule;
  file: string;
  line: number;
  /** The offending token or snippet. */
  token: string;
  message: string;
}

/** Variants that make a utility apply from 768 px up (rule 4's "md: or lg:"). */
const WIDE_VARIANTS = new Set(["md", "lg", "xl", "2xl"]);
/** Rule 4's limit in px. */
export const MAX_FIXED_WIDTH_PX = 360;
/** Rule 5: a fixed grid track wider than this, below md, is a side column. */
export const MAX_NARROW_TRACK_PX = 160;
/** Components that draw a chart or canvas sized to their container (rule 6). */
export const CHART_COMPONENTS = ["TimeSeriesChart", "ReactFlow"] as const;

/** Tailwind 4 container sizes (`w-sm` = `--container-sm`), in px. */
const CONTAINER_PX: Record<string, number> = {
  "3xs": 256, "2xs": 288, xs: 320, sm: 384, md: 448, lg: 512, xl: 576,
  "2xl": 672, "3xl": 768, "4xl": 896, "5xl": 1024, "6xl": 1152, "7xl": 1280,
};

function lineOf(text: string, index: number): number {
  let line = 1;
  for (let i = 0; i < index && i < text.length; i++) if (text.charCodeAt(i) === 10) line++;
  return line;
}

/** A length like `480px`, `22rem` or `30em` in px; null for anything else (`%`, `auto`, `1fr`, `var()`). */
export function lengthPx(value: string): number | null {
  const m = /^(\d+(?:\.\d+)?)(px|rem|em)$/.exec(value.trim());
  if (!m) return null;
  const n = Number(m[1]);
  return m[2] === "px" ? n : n * 16;
}

/** Splits a class token into its variants and the utility: `md:hover:w-96` → [["md","hover"], "w-96"]. */
export function splitVariants(token: string): { variants: string[]; utility: string } {
  // Colons inside brackets (`[&:hover]`, `grid-cols-[a:b]`) are not variant separators.
  const parts: string[] = [];
  let depth = 0;
  let current = "";
  for (const ch of token) {
    if (ch === "[" || ch === "(") depth++;
    else if (ch === "]" || ch === ")") depth--;
    if (ch === ":" && depth === 0) {
      parts.push(current);
      current = "";
    } else {
      current += ch;
    }
  }
  // The important modifier: `!w-96` (Tailwind 3) or `w-96!` (Tailwind 4).
  return { variants: parts, utility: current.replace(/^!/, "").replace(/!$/, "") };
}

const isWide = (variants: string[]) => variants.some((v) => WIDE_VARIANTS.has(v));

/** The fixed width a width utility sets, in px (`w-96` 384, `min-w-[22rem]` 352, `size-[400px]` 400); null if none. */
export function fixedWidthPx(utility: string): number | null {
  const m = /^(?:w|min-w|basis|size)-(.+)$/.exec(utility);
  if (!m) return null;
  const value = m[1];
  const arbitrary = /^\[(.+)\]$/.exec(value);
  if (arbitrary) return lengthPx(arbitrary[1]);
  if (/^\d+(\.\d+)?$/.test(value)) return Number(value) * 4;
  return CONTAINER_PX[value] ?? null;
}

interface Literal {
  text: string;
  index: number;
}

/** Every string literal and template-literal chunk in a TS/TSX source (class names live in these). */
export function stringLiterals(source: string): Literal[] {
  const out: Literal[] = [];
  const re = /"((?:[^"\\\n]|\\.)*)"|'((?:[^'\\\n]|\\.)*)'|`((?:[^`\\]|\\.)*)`/g;
  for (let m = re.exec(source); m; m = re.exec(source)) {
    const text = m[1] ?? m[2] ?? m[3] ?? "";
    // Template literals: the static class chunks, and (where statically visible) the string
    // literals inside each `${…}` (`${open ? "w-[480px]" : "w-0"}`) as literals of their own.
    out.push({ text: m[3] !== undefined ? text.replace(/\$\{[^}]*\}/g, " ") : text, index: m.index });
    if (m[3] !== undefined) {
      for (const expr of text.matchAll(/\$\{([^}]*)\}/g)) {
        for (const inner of stringLiterals(expr[1])) out.push({ text: inner.text, index: m.index });
      }
    }
  }
  return out;
}

const tokensOf = (text: string) => text.split(/\s+/).filter(Boolean);

/** Rules 1, 3, 4 and 5 on class tokens; "screen" on tokens and raw text. */
function checkClassLiterals(file: string, source: string, out: Violation[]): void {
  for (const literal of stringLiterals(source)) {
    const tokens = tokensOf(literal.text);
    const line = lineOf(source, literal.index);
    const utilities = tokens.map(splitVariants);

    // Rule 1: a horizontal flex grower must be allowed to shrink below its content. A vertical
    // grower (`min-h-0`, a column) is about height and is not checked.
    const grows = utilities.some(({ utility }) => utility === "flex-1" || utility === "grow" || utility === "flex-grow");
    if (grows) {
      const hasMinW = utilities.some(({ utility }) => utility.startsWith("min-w-"));
      const vertical = utilities.some(({ utility }) => utility === "min-h-0");
      if (!hasMinW && !vertical) {
        out.push({ rule: "R1", file, line, token: literal.text.trim(), message: "flex-1 child without min-w-0 (rule 1)" });
      }
    }

    for (const [i, { variants, utility }] of utilities.entries()) {
      const token = tokens[i];
      if (utility === "w-screen" || utility === "min-w-screen" || /\b100[dsl]?vw\b/.test(utility)) {
        out.push({ rule: "screen", file, line, token, message: "w-screen / 100vw makes the page wider than the viewport" });
        continue;
      }
      const px = fixedWidthPx(utility);
      if (px !== null && px > MAX_FIXED_WIDTH_PX && !isWide(variants)) {
        out.push({ rule: "R4", file, line, token, message: `fixed width ${px}px over ${MAX_FIXED_WIDTH_PX}px without md: or lg: (rule 4)` });
      }
      const cols = /^grid-cols-(\d+)$/.exec(utility);
      if (cols && Number(cols[1]) >= 3 && !isWide(variants) && !variants.includes("sm")) {
        out.push({ rule: "R3", file, line, token, message: "fixed multi-column grid at phone width; use repeat(auto-fit, minmax(min(100%, 11rem), 1fr)) (rule 3)" });
      }
      const template = /^grid-cols-\[(.+)\]$/.exec(utility);
      if (template && !isWide(variants)) {
        const tracks = template[1].replace(/minmax\([^)]*\)|repeat\([^)]*\)|min\([^)]*\)/g, "").split("_");
        const wide = tracks.map(lengthPx).find((n) => n !== null && n > MAX_NARROW_TRACK_PX);
        if (wide !== undefined) {
          out.push({ rule: "R5", file, line, token, message: `a ${wide}px side column below md (rule 5: side content becomes a Sheet below 1024 px)` });
        }
        if (/repeat\(\s*[3-9]/.test(template[1])) {
          out.push({ rule: "R3", file, line, token, message: "fixed repeat() column count at phone width (rule 3)" });
        }
      }
    }
  }
}

/** Rule 4 for inline styles (`style={{ width: 480 }}`, `minWidth: "30rem"`); not `max-width:` in a media query string. */
function checkInlineStyles(file: string, source: string, out: Violation[]): void {
  const re = /(?<![-\w])(width|minWidth|flexBasis|inlineSize|minInlineSize)\s*:\s*["'`]?(\d+(?:\.\d+)?)(px|rem|em)?\b/g;
  for (let m = re.exec(source); m; m = re.exec(source)) {
    const px = m[3] === "rem" || m[3] === "em" ? Number(m[2]) * 16 : Number(m[2]);
    if (px > MAX_FIXED_WIDTH_PX) {
      out.push({ rule: "R4", file, line: lineOf(source, m.index), token: m[0], message: `inline ${m[1]} ${px}px over ${MAX_FIXED_WIDTH_PX}px (rule 4)` });
    }
  }
  const vw = /\b100[dsl]?vw\b/g;
  for (let m = vw.exec(source); m; m = vw.exec(source)) {
    // Class tokens are reported by checkClassLiterals; this catches style objects (`width: "100vw"`).
    const before = source.slice(Math.max(0, source.lastIndexOf("\n", m.index)), m.index);
    if (/(width|minWidth|inlineSize|minInlineSize)\s*:\s*["'`]?$/.test(before)) {
      out.push({ rule: "screen", file, line: lineOf(source, m.index), token: m[0], message: `inline ${m[0]} makes the page wider than the viewport` });
    }
  }
}

/** Rule 2: a file that renders a <table> wraps it in an overflow-x-auto (or overflow-auto) scroller. */
function checkTables(file: string, source: string, out: Violation[]): void {
  const at = source.search(/<table[\s>]/);
  if (at === -1) return;
  if (!/\boverflow-(x-)?auto\b/.test(source)) {
    out.push({ rule: "R2", file, line: lineOf(source, at), token: "<table", message: "a table without an overflow-x-auto wrapper in the same file (rule 2)" });
  }
}

/** Rule 6: a file that renders a chart component gives it a min-w-0 container. */
function checkCharts(file: string, source: string, out: Violation[]): void {
  for (const name of CHART_COMPONENTS) {
    const at = source.search(new RegExp(`<${name}[\\s>/]`));
    if (at !== -1 && !/\bmin-w-0\b/.test(source)) {
      out.push({ rule: "R6", file, line: lineOf(source, at), token: `<${name}`, message: `<${name}> outside a min-w-0 container (rule 6)` });
    }
  }
  // An <svg viewBox> chart sizes to its container: width="100%" or w-full, never a fixed width.
  const svg = /<svg\b[^>]*\bviewBox=\{[^>]*>/g;
  for (let m = svg.exec(source); m; m = svg.exec(source)) {
    const tag = m[0];
    if (/aria-hidden/.test(tag) && !/width="100%"|\bw-full\b/.test(tag) && /\bwidth=\{?\s*["']?\d{3,}/.test(tag)) {
      out.push({ rule: "R6", file, line: lineOf(source, m.index), token: "<svg viewBox", message: "a chart svg with a fixed width; size it to its container (rule 6)" });
    }
  }
}

/** Rule 7: a file that opens a content popover (`<PopoverContent`) also has its Sheet for phones. */
function checkPopovers(file: string, source: string, out: Violation[]): void {
  const at = source.search(/<PopoverContent[\s>]/);
  if (at === -1) return;
  if (!/<Sheet(Content)?[\s>]/.test(source)) {
    out.push({ rule: "R7", file, line: lineOf(source, at), token: "<PopoverContent", message: "a popover with no Sheet below 640 px (rule 7)" });
  }
}

/** True for a media query that only applies from 768 px up (`min-width: 768px` / `48rem` or more). */
export function isWideMedia(query: string): boolean {
  if (/max-width/.test(query)) return false;
  const min = /min-width\s*:\s*(\d+(?:\.\d+)?(?:px|rem|em))/.exec(query);
  const px = min ? lengthPx(min[1]) : null;
  return px !== null && px >= 768;
}

/**
 * CSS: `100vw` (and dvw, svw, lvw) anywhere, and rule 4 for `width` / `min-width` /
 * `inline-size` declarations, unless inside a media block that applies only from 768 px up
 * (rule 4's `md:`); a `max-width` or narrower media block does not excuse them.
 */
function checkCss(file: string, source: string, out: Violation[]): void {
  let depth = 0;
  const media: Array<{ depth: number; wide: boolean }> = [];
  const lines = source.split("\n");
  lines.forEach((text, i) => {
    const line = i + 1;
    const code = text.replace(/\/\*.*?\*\//g, "");
    const query = /@media\b([^{]*)/.exec(code);
    if (query) media.push({ depth, wide: isWideMedia(query[1]) });
    const vw = /\b100[dsl]?vw\b/.exec(code);
    if (vw) out.push({ rule: "screen", file, line, token: vw[0], message: `${vw[0]} makes the page wider than the viewport` });
    const decl = /(?<![-\w])(min-)?(width|inline-size)\s*:\s*(\d+(?:\.\d+)?(?:px|rem|em))/.exec(code);
    if (decl && !media.some((block) => block.wide)) {
      const px = lengthPx(decl[3]);
      if (px !== null && px > MAX_FIXED_WIDTH_PX) out.push({ rule: "R4", file, line, token: decl[0], message: `CSS width ${px}px over ${MAX_FIXED_WIDTH_PX}px outside a min-width ≥ 768px media query (rule 4)` });
    }
    for (const ch of code) {
      if (ch === "{") depth++;
      else if (ch === "}") {
        depth--;
        if (media.length && depth === media[media.length - 1].depth) media.pop();
      }
    }
  });
}

/** All rule violations in one file's text. `file` is used for the report and to pick CSS or TS checks. */
export function checkLayout(file: string, source: string): Violation[] {
  const out: Violation[] = [];
  if (file.endsWith(".css")) {
    checkCss(file, source, out);
    return out;
  }
  checkClassLiterals(file, source, out);
  checkInlineStyles(file, source, out);
  checkTables(file, source, out);
  checkCharts(file, source, out);
  checkPopovers(file, source, out);
  return out;
}

/** A known exception: the rule, the file, a substring of the token, and why it is fine. */
export interface Allowed {
  rule: Exclude<LayoutRule, "R4" | "screen">;
  file: string;
  token: string;
  reason: string;
}

export function isAllowed(violation: Violation, allow: readonly Allowed[]): Allowed | undefined {
  return allow.find((entry) => entry.rule === violation.rule && entry.file === violation.file && violation.token.includes(entry.token));
}
