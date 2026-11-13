/**
 * Test-id inventory (decision "Tests during migration"): every id a test queries must be rendered by
 * some source file, so a renamed or dropped `data-testid` fails loudly instead of leaving a test that
 * silently checks nothing (`queryByTestId(...)` of a dead id is always null).
 *
 * Rendered ids come from source files:
 * - every string literal inside a `data-testid=` attribute (`"x"`, `{"x"}`, `{a ? "x" : "y"}`);
 * - every string literal passed through a test-id prop (`testId`, `idPrefix`), as JSX
 *   attribute or object key, since the component renders it as `data-testid={testId}`;
 * - template literals in either place: one with no `${…}` is an exact id; otherwise its static
 *   head (`workflow-row-` in `` `workflow-row-${id}` ``) is a dynamic prefix.
 * Dynamic prefixes only count when listed in an allowlist with a reason, so a loose prefix cannot
 * quietly cover a dead id.
 *
 * Queried ids come from test files: `ByTestId("x")` (exact), `` ByTestId(`x-${…}`) `` (prefix),
 * `ByTestId(/re/)` (regex), and `[data-testid="x"]` / `[data-testid^="x"]` selectors.
 * A query through a variable (`getByTestId(id)`) cannot be checked statically and is skipped.
 */

export interface SourceFile {
  path: string;
  text: string;
}

export interface RenderedIds {
  exact: Set<string>;
  /** Static heads of templated ids, mapped to the files that render them. */
  prefixes: Map<string, Set<string>>;
}

export type QueryKind = "exact" | "prefix" | "regex";

export interface TestIdQuery {
  kind: QueryKind;
  value: string;
  file: string;
  line: number;
}

export interface DeadQuery extends TestIdQuery {
  reason: string;
}

/** The expression after `=` / `:` at `start`: a quoted string, or a balanced `{…}` block. */
function readValue(text: string, start: number): string | null {
  let i = start;
  while (text[i] === " ") i++;
  const c = text[i];
  if (c === '"' || c === "'") {
    const end = text.indexOf(c, i + 1);
    return end < 0 ? null : text.slice(i, end + 1);
  }
  if (c === "`") {
    const end = scanTemplate(text, i);
    return end < 0 ? null : text.slice(i, end + 1);
  }
  if (c !== "{") return null;
  let depth = 0;
  for (let j = i; j < text.length; j++) {
    const ch = text[j];
    if (ch === '"' || ch === "'") {
      const end = text.indexOf(ch, j + 1);
      if (end < 0) return null;
      j = end;
    } else if (ch === "`") {
      const end = scanTemplate(text, j);
      if (end < 0) return null;
      j = end;
    } else if (ch === "{") depth++;
    else if (ch === "}") {
      depth--;
      if (depth === 0) return text.slice(i, j + 1);
    }
  }
  return null;
}

/** Index of the closing backtick of the template literal opening at `start` (handles `${…}`). */
function scanTemplate(text: string, start: number): number {
  let depth = 0;
  for (let j = start + 1; j < text.length; j++) {
    const ch = text[j];
    if (ch === "\\") {
      j++;
    } else if (depth === 0 && ch === "`") {
      return j;
    } else if (ch === "$" && text[j + 1] === "{") {
      depth++;
      j++;
    } else if (depth > 0 && ch === "}") {
      depth--;
    } else if (depth > 0 && ch === "`") {
      const end = scanTemplate(text, j);
      if (end < 0) return -1;
      j = end;
    }
  }
  return -1;
}

/** Exact ids and template heads among the literals of one expression. */
function literalsIn(expr: string): { exact: string[]; heads: string[] } {
  const exact: string[] = [];
  const heads: string[] = [];
  for (let i = 0; i < expr.length; i++) {
    const ch = expr[i];
    if (ch === '"' || ch === "'") {
      const end = expr.indexOf(ch, i + 1);
      if (end < 0) break;
      const value = expr.slice(i + 1, end);
      if (value) exact.push(value);
      i = end;
    } else if (ch === "`") {
      const end = scanTemplate(expr, i);
      if (end < 0) break;
      const body = expr.slice(i + 1, end);
      const hole = body.indexOf("${");
      if (hole < 0) {
        if (body) exact.push(body);
      } else if (hole > 0) {
        heads.push(body.slice(0, hole));
      } else {
        // `${x}…` - a pass-through id; recurse into nested templates of the hole only.
        literalsIn(body.slice(2)).heads.forEach((h) => heads.push(h));
      }
      i = end;
    }
  }
  return { exact, heads };
}

const RENDER_SITES = /(?:\bdata-testid|\b(?:testId|idPrefix))\s*(?:=|:)\s*/g;

/** Ids rendered by these source files (see module comment). */
export function collectRenderedIds(sources: SourceFile[]): RenderedIds {
  const exact = new Set<string>();
  const prefixes = new Map<string, Set<string>>();
  for (const { path, text } of sources) {
    for (const match of text.matchAll(RENDER_SITES)) {
      // Skip selectors (`[data-testid="x"]`) and type annotations (`testId: string`).
      if (text[match.index! - 1] === "[") continue;
      const value = readValue(text, match.index! + match[0].length);
      if (!value) continue;
      const add = (expr: string) => {
        const found = literalsIn(expr);
        found.exact.forEach((id) => exact.add(id));
        for (const head of found.heads) {
          if (!prefixes.has(head)) prefixes.set(head, new Set());
          prefixes.get(head)!.add(path);
        }
      };
      add(value);
      // `data-testid={id}` or `` {`${id}-x`} ``: resolve `const id = …` / `let id = …` in this file.
      for (const name of leadingIdentifiers(value)) {
        for (const decl of text.matchAll(new RegExp(`\\b(?:const|let)\\s+${name}\\s*=\\s*`, "g"))) {
          const init = readValue(text, decl.index! + decl[0].length);
          if (init) add(init);
        }
      }
    }
  }
  return { exact, prefixes };
}

/** Identifiers an id expression starts from: `{id}`, `{a ? b : c}` operands, and `` `${id}…` `` heads. */
function leadingIdentifiers(expr: string): string[] {
  const names = new Set<string>();
  const inner = expr.startsWith("{") ? expr.slice(1, -1) : expr;
  for (const m of inner.matchAll(/(?:^|[?:]\s*|`\$\{)\s*([A-Za-z_$][\w$]*)\s*(?=$|[?:}`]|\})/g)) names.add(m[1]);
  return [...names].filter((n) => !["undefined", "null", "true", "false"].includes(n));
}

const QUERY_SITES = /ByTestId\(\s*/g;
const SELECTOR_SITES = /\[data-testid(\^?)=(["'])([^"'\]]+)\2\]/g;

function lineOf(text: string, index: number): number {
  let line = 1;
  for (let i = 0; i < index; i++) if (text.charCodeAt(i) === 10) line++;
  return line;
}

/** Ids queried by these test files (see module comment). */
export function collectQueries(tests: SourceFile[]): TestIdQuery[] {
  const out: TestIdQuery[] = [];
  for (const { path, text } of tests) {
    for (const match of text.matchAll(QUERY_SITES)) {
      const at = match.index! + match[0].length;
      const line = lineOf(text, match.index!);
      const c = text[at];
      if (c === '"' || c === "'") {
        const end = text.indexOf(c, at + 1);
        if (end > at) out.push({ kind: "exact", value: text.slice(at + 1, end), file: path, line });
      } else if (c === "`") {
        const end = scanTemplate(text, at);
        if (end < 0) continue;
        const body = text.slice(at + 1, end);
        const hole = body.indexOf("${");
        if (hole < 0) out.push({ kind: "exact", value: body, file: path, line });
        else if (hole > 0) out.push({ kind: "prefix", value: body.slice(0, hole), file: path, line });
      } else if (c === "/") {
        const m = /^\/((?:\\.|[^/\\\n])+)\/[a-z]*/.exec(text.slice(at));
        if (m) out.push({ kind: "regex", value: m[1], file: path, line });
      }
    }
    for (const match of text.matchAll(SELECTOR_SITES)) {
      out.push({
        kind: match[1] ? "prefix" : "exact",
        value: match[3],
        file: path,
        line: lineOf(text, match.index!),
      });
    }
  }
  return out;
}

/** Queries that no source renders. `allowedPrefixes` are the source's dynamic prefixes that may match. */
export function findDeadQueries(
  queries: TestIdQuery[],
  rendered: RenderedIds,
  allowedPrefixes: Iterable<string>,
  /** Prefixes of ids rendered by third-party components (not in our source), allowed as-is. */
  externalPrefixes: Iterable<string> = [],
  /** Ids a test file renders itself (harness markup), keyed by its path: valid only for that file's queries. */
  localIds: Map<string, RenderedIds> = new Map(),
): DeadQuery[] {
  const allowed = [...[...allowedPrefixes].filter((p) => rendered.prefixes.has(p)), ...externalPrefixes];
  const dead: DeadQuery[] = [];
  for (const q of queries) {
    const local = localIds.get(q.file);
    if (local && (local.exact.has(q.value) || [...local.prefixes.keys()].some((p) => q.value.startsWith(p)))) continue;
    if (q.kind === "exact") {
      if (rendered.exact.has(q.value) || allowed.some((p) => q.value.startsWith(p))) continue;
      const near = [...rendered.prefixes.keys()].filter((p) => q.value.startsWith(p));
      dead.push({
        ...q,
        reason: near.length
          ? `only a dynamic prefix not in the allowlist matches (${near.join(", ")}); add it with a reason`
          : "no source file renders this id",
      });
    } else if (q.kind === "prefix") {
      const ok =
        [...rendered.exact].some((id) => id.startsWith(q.value)) ||
        allowed.some((p) => p.startsWith(q.value) || q.value.startsWith(p));
      if (!ok) dead.push({ ...q, reason: "no rendered id or allowed prefix starts with this" });
    } else {
      let re: RegExp;
      try {
        re = new RegExp(q.value);
      } catch {
        dead.push({ ...q, reason: "invalid regular expression" });
        continue;
      }
      const head = /^\^?([\w-]*)/.exec(q.value)![1];
      const ok =
        [...rendered.exact].some((id) => re.test(id)) ||
        (head !== "" && allowed.some((p) => p.startsWith(head) || head.startsWith(p)));
      if (!ok) dead.push({ ...q, reason: "no rendered id matches this pattern" });
    }
  }
  return dead;
}
