// Citation links in assistant text (RM.16). An agent with knowledge bases gets the `retrieve` tool;
// the model cites its numbered sources as `[n]`. The turn's `tool_result` event of that call carries
// the structured sources `{n, title, url, ref, kb_id, document_id, document_key, chunk_id, score}`.
//
// Security rule (RAG plan, RM.16): a citation's link comes ONLY from `sources[n]` of the turn's
// retrieve results, never from a URL in the model's text. Retrieved text is tenant content and the
// model can be steered by it, so `[1]: https://evil` definitions, `[1](https://evil)` and
// `[[1]](https://evil)` in the answer are rewritten to the real source (or to plain `[n]` when no
// source has that number). A source URL is linked only when it is an absolute http(s) URL; a source
// without one links to its document in Studio.
import { isSafeHttpUrl } from "@/lib/safeUrl";
import { routes } from "@/lib/routes";
import type { ToolCallInfo } from "./toolCalls";

/** The name of the platform retrieval tool (eap_worker.platform_tools.RETRIEVE_TOOL_NAME). */
export const RETRIEVE_TOOL_NAME = "retrieve";

export interface CitationSource {
  n: number;
  title: string;
  url: string | null;
  ref?: string;
  kb_id?: string;
  document_id?: string;
  document_key?: string;
  chunk_id?: string;
  doc_version?: number;
  score?: number;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/** The well-formed sources of a retrieve `tool_result` event's `sources` (anything else dropped). */
export function parseSources(raw: unknown): CitationSource[] {
  if (!Array.isArray(raw)) return [];
  const out: CitationSource[] = [];
  for (const item of raw) {
    if (!isRecord(item)) continue;
    const n = item.n;
    if (typeof n !== "number" || !Number.isInteger(n) || n < 1) continue;
    out.push({
      n,
      title: typeof item.title === "string" && item.title ? item.title : `Source ${n}`,
      url: typeof item.url === "string" ? item.url : null,
      ref: typeof item.ref === "string" ? item.ref : undefined,
      kb_id: typeof item.kb_id === "string" ? item.kb_id : undefined,
      document_id: typeof item.document_id === "string" ? item.document_id : undefined,
      document_key: typeof item.document_key === "string" ? item.document_key : undefined,
      chunk_id: typeof item.chunk_id === "string" ? item.chunk_id : undefined,
      doc_version: typeof item.doc_version === "number" ? item.doc_version : undefined,
      score: typeof item.score === "number" ? item.score : undefined,
    });
  }
  return out;
}

/**
 * `[n]` → source for one turn, or null when the turn made no retrieve call (its text is then left
 * as it is). The sources are the LAST retrieve call's (in the turn's order) that returned any: each
 * call numbers its sources from 1 and the answer follows the latest results, so earlier calls'
 * numbers are not mixed in (a mixed map could link `[2]` to another call's source). `infos` is the
 * chat's map keyed by `toolCallInfoKey(runId, callId)` = `${runId}:${callId}`.
 */
export function citationSourcesForRun(infos: ReadonlyMap<string, ToolCallInfo>, runId: string): Map<number, CitationSource> | null {
  const prefix = `${runId}:`;
  let retrieved = false;
  let latest: CitationSource[] = [];
  for (const [key, info] of infos) {
    if (!key.startsWith(prefix) || !info.sources) continue;
    retrieved = true;
    if (info.sources.length > 0) latest = info.sources;
  }
  if (!retrieved) return null;
  const map = new Map<number, CitationSource>();
  for (const source of latest) if (!map.has(source.n)) map.set(source.n, source);
  return map;
}

/**
 * Where `[n]` goes: the source's http(s) URL, else its document in Studio (the knowledge base page
 * with the document Sheet open), else nowhere (null: the marker stays text).
 */
export function citationHref(source: CitationSource): string | null {
  if (isSafeHttpUrl(source.url)) return source.url;
  if (source.kb_id && source.document_key) return routes.knowledgeBase(source.kb_id, { document: source.document_key });
  return null;
}

// ---------------------------------------------------------------------------------------------
// The remark plugin (mdast; types kept local: only the node shapes used here)

interface MdNode {
  type: string;
  value?: string;
  url?: string;
  label?: string | null;
  alt?: string | null;
  identifier?: string;
  children?: MdNode[];
  data?: { hProperties?: Record<string, string> };
}

/** The attribute that marks a citation link for the Markdown `a` renderer. */
export const CITATION_ATTR = "data-citation";

const MARKER = /\[(\d{1,3})\]/g;
/**
 * Characters a reader does not see as text: default-ignorable code points (format characters,
 * zero-width joiners, variation selectors such as U+FE0F, tag characters U+E0000–E007F, U+034F) and
 * combining marks. Built with the RegExp constructor: `\p{…}` needs the `u` flag at runtime, which
 * every supported browser has, while the TypeScript target (ES2017) refuses it in a literal.
 */
const INVISIBLE = new RegExp("[\\p{Default_Ignorable_Code_Point}\\p{Mn}\\p{Me}]", "gu");
/** Any letter, in any script: a link with one is ordinary text ("step 1"), not a citation. */
const LETTER = new RegExp("\\p{L}", "u");
/** Runs of decimal digits in any script (`1`, `١`, `१`). */
const DIGIT_RUN = new RegExp("\\p{Nd}+", "gu");
const IS_DIGIT = new RegExp("^\\p{Nd}$", "u");

/** The value of one decimal digit of any script: its offset in its block of ten (Unicode keeps them contiguous). */
function digitValue(char: string): number {
  const cp = char.codePointAt(0)!;
  let start = cp;
  while (start > 0 && cp - start < 9 && IS_DIGIT.test(String.fromCodePoint(start - 1))) start -= 1;
  return (cp - start) % 10;
}

/** The numbers written in `text`, in order (any script's digits); null for one over 3 digits. */
export function numbersIn(text: string): Array<number | null> {
  return Array.from(text.matchAll(DIGIT_RUN), (match) => {
    const digits = Array.from(match[0]);
    return digits.length > 3 ? null : digits.reduce((value, digit) => value * 10 + digitValue(digit), 0);
  });
}

/**
 * Link text as a reader sees it: NFKC (fullwidth `［１］` → `[1]`) without invisible characters
 * (`[1]\u200B`, `1\uFE0F` → `[1]`, `1`), trimmed. Used only to decide what a model-written link
 * LOOKS like; the rendered text is always our own.
 */
export function normalizeMarker(text: string): string {
  return text.normalize("NFKC").replace(INVISIBLE, "").trim();
}

function plainText(node: MdNode): string {
  if (typeof node.value === "string") return node.value;
  // An image shows its alt text (MarkdownText never renders <img>), so it counts as text here.
  if (node.type === "image" || node.type === "imageReference") return node.alt ?? "";
  return (node.children ?? []).map(plainText).join("");
}

function citationNode(n: number, sources: ReadonlyMap<number, CitationSource>): MdNode {
  const source = sources.get(n);
  const href = source ? citationHref(source) : null;
  if (!source || !href) return { type: "text", value: `[${n}]` };
  return { type: "link", url: href, children: [{ type: "text", value: `[${n}]` }], data: { hProperties: { [CITATION_ATTR]: String(n) } } };
}

function splitText(value: string, sources: ReadonlyMap<number, CitationSource>): MdNode[] {
  const out: MdNode[] = [];
  let last = 0;
  for (const match of value.matchAll(MARKER)) {
    const start = match.index ?? 0;
    if (start > last) out.push({ type: "text", value: value.slice(last, start) });
    out.push(citationNode(Number(match[1]), sources));
    last = start + match[0].length;
  }
  if (last === 0) return [{ type: "text", value }];
  if (last < value.length) out.push({ type: "text", value: value.slice(last) });
  return out;
}

function transform(node: MdNode, sources: ReadonlyMap<number, CitationSource>): void {
  if (!node.children) return;
  const next: MdNode[] = [];
  for (const child of node.children) {
    if (child.type === "code" || child.type === "inlineCode" || child.type === "html") {
      next.push(child);
    } else if (child.type === "link" || child.type === "linkReference") {
      // In a turn that called retrieve, a model-written link with no letter in its visible text
      // (`[1]`, `(1)`, `【1】`, `[١]`, `[1, 2]`, `→`) reads as a citation, so it never keeps the
      // model's URL: each number maps to its source, or stays plain `[n]` text; with no number the
      // visible text stays as plain text. Links with words ("step 1") are ordinary links.
      const visible = normalizeMarker(plainText(child));
      if (LETTER.test(visible)) {
        next.push(child); // an ordinary link: its children are left alone (no nested links)
      } else {
        let numbers = numbersIn(visible);
        if (numbers.length === 0 && child.type === "linkReference") numbers = numbersIn(normalizeMarker(child.label ?? child.identifier ?? ""));
        if (numbers.length > 0 && numbers.every((n) => n !== null)) {
          for (const n of numbers) next.push(citationNode(n as number, sources));
        } else if (visible) {
          next.push({ type: "text", value: visible });
        }
      }
    } else if (child.type === "text" && typeof child.value === "string") {
      next.push(...splitText(child.value, sources));
    } else {
      transform(child, sources);
      next.push(child);
    }
  }
  node.children = next;
}

/** A remark plugin: `[n]` markers in assistant text become links to `sources[n]` (see the module comment). */
export function remarkCitations(sources: ReadonlyMap<number, CitationSource>) {
  return () => (tree: MdNode) => {
    transform(tree, sources);
  };
}
