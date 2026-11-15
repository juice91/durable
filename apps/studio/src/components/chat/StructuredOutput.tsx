"use client";

import React, { useState } from "react";
import { Braces, Check, Copy } from "lucide-react";
import { cn } from "@/lib/cn";
import { ICON_STROKE_WIDTH } from "@/lib/icons";
import { copyText } from "@/lib/clipboard";
import { focusRing } from "@/components/ui/Button";
import type { CitationSource } from "./citations";
import { CitationChip, CitationMarkdown } from "./messageParts";

// The shared view of a structured output (AC.14): the agent Chat tab's answer, the trace Sheet's
// Events tab (`run_completed.output_json`) and a workflow agent step's output (StepIoPanel). The
// object renders as a tree; string leaves render as safe Markdown in which `[n]` links to
// `sources[n]` of the turn's retrieve results (citations.ts: never to a URL in the output), and an
// integer list under a `citations` key renders as citation links. Copy puts the JSON on the clipboard.
// The inner test ids are fixed (`structured-output-copy`, `-raw-toggle`, `-json`, `-copy-status`):
// query them within the view's own `testId`.

/** Nesting below this depth is shown as JSON text (a pathological object never blows the stack). */
const MAX_DEPTH = 12;

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

export function prettyJson(value: unknown): string {
  try {
    return JSON.stringify(value, null, 2) ?? String(value);
  } catch {
    return String(value);
  }
}

function isCitationList(key: string | null, value: unknown[]): value is number[] {
  return key === "citations" && value.length > 0 && value.every((n) => typeof n === "number" && Number.isInteger(n) && n >= 1);
}

const leafText = "font-mono text-caption text-foreground";
const stringClass = "min-w-0 break-words text-body leading-relaxed [&_a]:text-info-text [&_a]:underline [&_a]:underline-offset-2 [&_code]:rounded-sm [&_code]:bg-fill-hover [&_code]:px-1 [&_code]:font-mono [&_code]:text-caption [&_ul]:list-disc [&_ul]:pl-5 [&_ol]:list-decimal [&_ol]:pl-5";

/** A string leaf longer than this is collapsed to its first `PREVIEW_CHARS` behind a toggle. */
export const LONG_STRING_CHARS = 500;
const PREVIEW_CHARS = 280;

/**
 * The preview of a long string: cut at the last whitespace before `PREVIEW_CHARS` (never mid-word,
 * so `[n]` stays whole), and never inside a Markdown link or image (`[text](url)`, `![alt](url)`):
 * an unclosed `[` or `(` before the cut moves the cut back to before it. `plain` is set when no
 * safe cut exists (the text opens with one long link or token): the preview is then shown as plain
 * text, so no half link is ever parsed as Markdown.
 */
export function stringPreview(text: string): { text: string; plain: boolean } {
  let cut = text.slice(0, PREVIEW_CHARS);
  const space = cut.search(/\s\S*$/);
  if (space > 0) cut = cut.slice(0, space);
  // Back off past any link that is not closed inside the preview.
  for (;;) {
    const open = Math.max(cut.lastIndexOf("["), cut.lastIndexOf("("));
    if (open < 0) break;
    const closer = cut[open] === "[" ? "]" : ")";
    const tail = cut.slice(open);
    const closedHere = tail.includes(closer) && !(cut[open] === "[" && /\]\([^)]*$/.test(tail));
    if (closedHere) break;
    let start = open;
    // `[text](url` cut inside the url: drop the `[text]` as well.
    if (cut[open] === "(" && cut[open - 1] === "]") start = Math.max(0, cut.lastIndexOf("[", open - 1));
    cut = cut.slice(0, cut[start - 1] === "!" ? start - 1 : start);
  }
  cut = cut.trimEnd();
  if (cut.length < PREVIEW_CHARS / 4) return { text: `${text.slice(0, PREVIEW_CHARS)}…`, plain: true };
  return { text: `${cut}…`, plain: false };
}

function LongString({ text, sources }: { text: string; sources: ReadonlyMap<number, CitationSource> | null }) {
  const [open, setOpen] = useState(false);
  const id = React.useId();
  const preview = React.useMemo(() => stringPreview(text), [text]);
  return (
    <div className="min-w-0" data-testid="structured-output-long-string" data-expanded={open}>
      <div id={id}>
        {open ? (
          <CitationMarkdown text={text} sources={sources} className={stringClass} />
        ) : preview.plain ? (
          <p className={cn(stringClass, "whitespace-pre-wrap")}>{preview.text}</p>
        ) : (
          <CitationMarkdown text={preview.text} sources={sources} className={stringClass} />
        )}
      </div>
      <button
        type="button"
        aria-expanded={open}
        aria-controls={id}
        onClick={() => setOpen((v) => !v)}
        className={cn("mt-0.5 rounded-sm text-caption text-info-text underline-offset-2 hover:underline", focusRing)}
      >
        {open ? "Show less" : `Show all (${text.length.toLocaleString("en-US")} characters)`}
      </button>
    </div>
  );
}

function JsonNode({
  value,
  keyName,
  path,
  depth,
  sources,
}: {
  value: unknown;
  keyName: string | null;
  path: string;
  depth: number;
  sources: ReadonlyMap<number, CitationSource> | null;
}) {
  if (typeof value === "string") {
    return value === "" ? (
      <span className={cn(leafText, "text-muted-foreground")}>{"\"\""}</span>
    ) : value.length > LONG_STRING_CHARS ? (
      <LongString text={value} sources={sources} />
    ) : (
      <CitationMarkdown text={value} sources={sources} className={stringClass} />
    );
  }
  if (value === null || typeof value !== "object") {
    return <span className={leafText}>{value === undefined ? "—" : String(value)}</span>;
  }
  if (depth >= MAX_DEPTH) return <pre className={cn(leafText, "whitespace-pre-wrap break-words")}>{prettyJson(value)}</pre>;
  if (Array.isArray(value)) {
    if (value.length === 0) return <span className={cn(leafText, "text-muted-foreground")}>[]</span>;
    if (isCitationList(keyName, value)) {
      return (
        <span data-testid="structured-output-citations" className="inline-flex flex-wrap items-baseline">
          {value.map((n, i) => (
            <CitationChip key={`${n}-${i}`} n={n} sources={sources} />
          ))}
        </span>
      );
    }
    return (
      <ol className="min-w-0 space-y-1">
        {value.map((item, i) => (
          <li key={i} className="flex min-w-0 gap-2" data-testid="structured-output-item" data-path={`${path}[${i}]`}>
            <span className="shrink-0 font-mono text-caption tabular-nums text-muted-foreground">{i}</span>
            <div className="min-w-0 flex-1">
              <JsonNode value={item} keyName={null} path={`${path}[${i}]`} depth={depth + 1} sources={sources} />
            </div>
          </li>
        ))}
      </ol>
    );
  }
  const entries = Object.entries(value as Record<string, unknown>);
  if (entries.length === 0) return <span className={cn(leafText, "text-muted-foreground")}>{"{}"}</span>;
  return (
    <dl className={cn("min-w-0 space-y-1.5", depth > 0 && "border-l border-border pl-3")}>
      {entries.map(([key, item]) => {
        const childPath = path ? `${path}.${key}` : key;
        const nested = isRecord(item) || (Array.isArray(item) && item.length > 0 && !isCitationList(key, item));
        return (
          <div key={key} data-testid="structured-output-field" data-path={childPath} className={cn("min-w-0", nested ? "space-y-1" : "flex flex-wrap items-baseline gap-x-2")}>
            <dt className="shrink-0 font-mono text-caption font-medium text-muted-foreground">{key}</dt>
            <dd className="min-w-0 flex-1">
              <JsonNode value={item} keyName={key} path={childPath} depth={depth + 1} sources={sources} />
            </dd>
          </div>
        );
      })}
    </dl>
  );
}

export interface StructuredOutputViewProps {
  value: unknown;
  /** The turn's retrieve sources (`[n]` links); null or absent: string leaves keep `[n]` as text. */
  sources?: ReadonlyMap<number, CitationSource> | null;
  label?: string;
  testId?: string;
  className?: string;
}

/** A structured output as a tree with Copy and a JSON toggle (see the module comment). */
export function StructuredOutputView({ value, sources = null, label = "Structured output", testId = "structured-output", className }: StructuredOutputViewProps) {
  const [raw, setRaw] = useState(false);
  const [copied, setCopied] = useState<string | null>(null);
  const copy = async () => {
    setCopied((await copyText(prettyJson(value))) ? "Copied" : "Copying is not available here");
  };
  const buttonClass = cn(
    "inline-flex items-center gap-1 rounded-sm px-1.5 py-0.5 text-caption text-muted-foreground hover:bg-fill-hover hover:text-foreground",
    focusRing,
  );
  return (
    <section
      data-testid={testId}
      data-output-format="json"
      aria-label={label}
      className={cn("my-2 min-w-0 rounded-md border border-border bg-surface text-foreground", className)}
    >
      <div className="flex min-w-0 items-center gap-2 border-b border-border px-3 py-1.5">
        <Braces strokeWidth={ICON_STROKE_WIDTH} aria-hidden="true" className="size-icon shrink-0 text-muted-foreground" />
        <h4 className="min-w-0 flex-1 truncate text-caption font-medium text-muted-foreground">{label}</h4>
        <button type="button" className={buttonClass} aria-pressed={raw} onClick={() => setRaw((v) => !v)} data-testid="structured-output-raw-toggle">
          JSON
        </button>
        <button type="button" className={buttonClass} onClick={copy} data-testid="structured-output-copy">
          {copied === "Copied" ? (
            <Check strokeWidth={ICON_STROKE_WIDTH} aria-hidden="true" className="size-icon" />
          ) : (
            <Copy strokeWidth={ICON_STROKE_WIDTH} aria-hidden="true" className="size-icon" />
          )}
          Copy
        </button>
      </div>
      <span role="status" className="sr-only" data-testid="structured-output-copy-status">
        {copied ?? ""}
      </span>
      <div className="min-w-0 px-3 py-2">
        {raw ? (
          <pre data-testid="structured-output-json" className="max-h-96 overflow-auto whitespace-pre-wrap break-words font-mono text-caption text-foreground">
            {prettyJson(value)}
          </pre>
        ) : (
          <JsonNode value={value} keyName={null} path="" depth={0} sources={sources} />
        )}
      </div>
    </section>
  );
}
