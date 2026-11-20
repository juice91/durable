"use client";

import { CopyableText } from "@/components/ui/CopyableText";
import React, { createContext, useContext, useMemo, useState } from "react";
import Link from "next/link";
import { useAuiState, type TextMessagePartProps, type ToolCallMessagePartProps } from "@assistant-ui/react";
import { type Components } from "react-markdown";
import { ChevronRight, Wrench } from "lucide-react";
import { cn } from "@/lib/cn";
import { ICON_STROKE_WIDTH } from "@/lib/icons";
import { Badge, type BadgeProps } from "@/components/ui/Badge";
import { focusRing } from "@/components/ui/Button";
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "@/components/ui/Collapsible";
import { SafeLink, SafeMarkdown } from "@/components/markdown/SafeMarkdown";
import { ToolDecisionView } from "./ToolDecisionView";
import type { ToolDecision } from "./turnCapabilities";
import { CITATION_ATTR, citationHref, citationSourcesForRun, citationTitle, pageLabel, type CitationSource } from "./citations";
import { formatDuration, messageRunId, toolCallInfoKey, TOOL_STATUS_LABELS, type ToolCallInfo, type ToolCallStatus } from "./toolCalls";

/**
 * `toolCallInfoKey(runId, toolCallId)` → its status and duration (see toolCalls.ts); the chat
 * provides it per thread. Keyed by run as well: a tool call id is unique only within its run (a
 * scripted model reuses ids per tool name across turns).
 */
export const ToolCallInfoContext = createContext<ReadonlyMap<string, ToolCallInfo>>(new Map());

/**
 * Thread message id → the native run id of its turn, when message ids do not carry it. The native
 * runtime's ids are `<runId>:...` (`messageRunId`); the AG-UI runtime's are the server's D13 ids or
 * the client's, so the AG-UI chat provides this map (AGUI.12). Null: parse the id.
 */
export const MessageRunsContext = createContext<ReadonlyMap<string, string> | null>(null);

/** A gated call's recorded decision, shown under its tool card (the AG-UI chat, AGUI.12). */
export interface ToolDecisionEntry {
  toolName: string;
  decision: ToolDecision;
  lostRace: boolean;
}

/**
 * `toolCallInfoKey(runId, toolCallId)` → the call's decision line. Provided by the AG-UI chat only:
 * the native chat renders the same line as its own data part (ToolApprovalPart).
 */
export const ToolDecisionsContext = createContext<ReadonlyMap<string, ToolDecisionEntry> | null>(null);

/** The native run id of the current message's turn, or null. */
export function useMessageRunId(): string | null {
  const messageId = useAuiState((s) => s.message.id);
  const runs = useContext(MessageRunsContext);
  if (runs) return runs.get(messageId) ?? null;
  return messageRunId(messageId);
}

const STATUS_VARIANT: Record<ToolCallStatus, BadgeProps["variant"]> = {
  running: "info",
  waiting: "warning",
  denied: "warning",
  ok: "success",
  error: "destructive",
  no_result: "neutral",
};

function json(value: unknown): string {
  if (typeof value === "string") return value;
  try {
    return JSON.stringify(value, null, 2) ?? String(value);
  } catch {
    return String(value);
  }
}

const monoBlock = "max-h-64 overflow-auto whitespace-pre-wrap break-words rounded-sm bg-fill-hover p-2 font-mono text-caption text-foreground";

/**
 * One tool call as a collapsible card: the tool name, its status and duration in the header;
 * arguments and result in mono inside. Collapsed by default.
 */
export function ToolCallCard({ toolCallId, toolName, args, result }: ToolCallMessagePartProps) {
  const runId = useMessageRunId();
  const infos = useContext(ToolCallInfoContext);
  const info = runId === null ? undefined : infos.get(toolCallInfoKey(runId, toolCallId));
  const status: ToolCallStatus = info?.status ?? (result !== undefined ? "ok" : "running");
  const [open, setOpen] = useState(false);
  const decisions = useContext(ToolDecisionsContext);
  const decision = runId === null ? undefined : decisions?.get(toolCallInfoKey(runId, toolCallId));
  return (
    <>
    <Collapsible open={open} onOpenChange={setOpen} asChild>
      <div data-testid="tool-call-card" data-status={status} className="my-2 min-w-0 rounded-md border border-border bg-surface text-foreground">
        <CollapsibleTrigger
          className={cn("flex w-full min-w-0 items-center gap-2 rounded-md px-3 py-2 text-left text-label hover:bg-fill-hover", focusRing)}
        >
          <ChevronRight
            strokeWidth={ICON_STROKE_WIDTH}
            aria-hidden="true"
            className={cn("size-icon shrink-0 text-muted-foreground transition-transform motion-reduce:transition-none", open && "rotate-90")}
          />
          <Wrench strokeWidth={ICON_STROKE_WIDTH} aria-hidden="true" className="size-icon shrink-0 text-muted-foreground" />
          <span className="min-w-0 flex-1 truncate font-mono font-medium">{toolName}</span>
          <Badge variant={STATUS_VARIANT[status]} data-testid="tool-call-status">
            {TOOL_STATUS_LABELS[status]}
          </Badge>
          {info?.durationMs !== null && info?.durationMs !== undefined ? (
            <span data-testid="tool-call-duration" className="shrink-0 text-caption tabular-nums text-muted-foreground">
              {formatDuration(info.durationMs)}
            </span>
          ) : null}
        </CollapsibleTrigger>
        <CollapsibleContent className="space-y-2 border-t border-border px-3 py-2">
          <div>
            <p className="mb-1 text-caption font-medium text-muted-foreground">Arguments</p>
            <CopyableText label="arguments" copyText={json(args)}>
              <pre data-testid="tool-call-args" className={`${monoBlock} pr-9`}>
                {json(args)}
              </pre>
            </CopyableText>
          </div>
          {result !== undefined ? (
            <div>
              <p className="mb-1 text-caption font-medium text-muted-foreground">Result</p>
              <CopyableText label="result" copyText={json(result)}>
                <pre data-testid="tool-call-result" className={`${monoBlock} pr-9`}>
                  {json(result)}
                </pre>
              </CopyableText>
            </div>
          ) : null}
        </CollapsibleContent>
      </div>
    </Collapsible>
    {decision ? <ToolDecisionView toolName={decision.toolName} toolCallId={toolCallId} decision={decision.decision} lostRace={decision.lostRace} /> : null}
    </>
  );
}

const citationClass = cn(
  "mx-0.5 inline-block rounded-sm px-0.5 align-baseline font-mono text-caption font-medium text-info-text",
  focusRing,
);

/** One `[n]` citation: a link to `sources[n]` (an http(s) URL in a new tab, or its document in Studio). */
function CitationLink({ n, href, source }: { n: string; href: string; source: CitationSource | undefined }) {
  const label = source ? `Source ${n}: ${citationTitle(source)}` : `Source ${n}`;
  const title = source ? `${citationTitle(source)}${source.url ? ` — ${source.url}` : ""}` : undefined;
  // A PDF section's page, after the marker: "[3] p. 212".
  const pageText = source ? pageLabel(source) : null;
  const page = pageText ? <span className="ml-1" data-testid="citation-page">{pageText}</span> : null;
  if (/^https?:\/\//i.test(href)) {
    return (
      <a href={href} target="_blank" rel="noopener noreferrer nofollow" data-testid="citation-link" data-citation={n} aria-label={label} title={title} className={citationClass}>
        [{n}]{page}
      </a>
    );
  }
  return (
    <Link href={href} data-testid="citation-link" data-citation={n} aria-label={label} title={title} className={citationClass}>
      [{n}]{page}
    </Link>
  );
}

/** `[n]` → source for the message's turn (from its retrieve results), or null when it retrieved nothing. */
export function useTurnSources(): Map<number, CitationSource> | null {
  const runId = useMessageRunId();
  const infos = useContext(ToolCallInfoContext);
  return useMemo(() => (runId === null ? null : citationSourcesForRun(infos, runId)), [infos, runId]);
}

/** One `[n]` citation chip outside Markdown (a structured `citations: [n, …]` field, AC.14). */
export function CitationChip({ n, sources }: { n: number; sources: ReadonlyMap<number, CitationSource> | null }) {
  const source = sources?.get(n);
  const href = source ? citationHref(source) : null;
  if (!source || !href) return <span className="mx-0.5 font-mono text-caption text-muted-foreground">[{n}]</span>;
  return <CitationLink n={String(n)} href={href} source={source} />;
}

export const markdownClass = cn(
  "min-w-0 space-y-2 break-words text-body leading-relaxed",
  "[&_a]:text-info-text [&_a]:underline [&_a]:underline-offset-2",
  "[&_code]:rounded-sm [&_code]:bg-fill-hover [&_code]:px-1 [&_code]:font-mono [&_code]:text-caption",
  "[&_pre]:overflow-x-auto [&_pre]:rounded-md [&_pre]:border [&_pre]:border-border [&_pre]:bg-fill-hover [&_pre]:p-3",
  "[&_pre_code]:bg-transparent [&_pre_code]:p-0",
  "[&_ul]:list-disc [&_ul]:pl-5 [&_ol]:list-decimal [&_ol]:pl-5",
  "[&_blockquote]:border-l-2 [&_blockquote]:border-border [&_blockquote]:pl-3 [&_blockquote]:text-muted-foreground",
  "[&_table]:block [&_table]:max-w-full [&_table]:overflow-x-auto [&_td]:border [&_td]:border-border [&_td]:px-2 [&_th]:border [&_th]:border-border [&_th]:px-2",
);

/**
 * Text as Markdown (GFM, no raw HTML, images as their alt text only) where `[n]` markers link to
 * `sources` (citations.ts), never to a URL written in the text. Null sources: no citation rewriting.
 */
export function CitationMarkdown({ text, sources, className }: { text: string; sources: ReadonlyMap<number, CitationSource> | null; className?: string }) {
  // Images (alt text only), raw HTML and URL rules come from SafeMarkdown; only citations add an `a`.
  const components = useMemo<Omit<Components, "img"> | undefined>(
    () =>
      sources
        ? {
            a: ({ node: _node, children, href, title, ...props }) => {
              const n = (props as Record<string, unknown>)[CITATION_ATTR];
              if (typeof n === "string" && href) return <CitationLink n={n} href={href} source={sources.get(Number(n))} />;
              return <SafeLink href={href} title={title}>{children}</SafeLink>;
            },
          }
        : undefined,
    [sources],
  );
  return (
    <div className={className ?? markdownClass}>
      <SafeMarkdown citations={sources} components={components}>
        {text}
      </SafeMarkdown>
    </div>
  );
}

/**
 * Assistant text as Markdown, styled with theme tokens. In a turn that called the `retrieve` tool,
 * `[n]` markers link to that call's structured sources (citations.ts), never to a URL in the text.
 */
export function MarkdownText({ text }: TextMessagePartProps) {
  const sources = useTurnSources();
  return <CitationMarkdown text={text} sources={sources} />;
}
