"use client";

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
import { CITATION_ATTR, citationSourcesForRun, citationTitle, pageLabel, type CitationSource } from "./citations";
import { formatDuration, messageRunId, toolCallInfoKey, TOOL_STATUS_LABELS, type ToolCallInfo, type ToolCallStatus } from "./toolCalls";

/**
 * `toolCallInfoKey(runId, toolCallId)` → its status and duration (see toolCalls.ts); the chat
 * provides it per thread. Keyed by run as well: a tool call id is unique only within its run (a
 * scripted model reuses ids per tool name across turns).
 */
export const ToolCallInfoContext = createContext<ReadonlyMap<string, ToolCallInfo>>(new Map());

const STATUS_VARIANT: Record<ToolCallStatus, BadgeProps["variant"]> = {
  running: "info",
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
  const messageId = useAuiState((s) => s.message.id);
  const runId = messageRunId(messageId);
  const infos = useContext(ToolCallInfoContext);
  const info = runId === null ? undefined : infos.get(toolCallInfoKey(runId, toolCallId));
  const status: ToolCallStatus = info?.status ?? (result !== undefined ? "ok" : "running");
  const [open, setOpen] = useState(false);
  return (
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
            <pre data-testid="tool-call-args" className={monoBlock}>
              {json(args)}
            </pre>
          </div>
          {result !== undefined ? (
            <div>
              <p className="mb-1 text-caption font-medium text-muted-foreground">Result</p>
              <pre data-testid="tool-call-result" className={monoBlock}>
                {json(result)}
              </pre>
            </div>
          ) : null}
        </CollapsibleContent>
      </div>
    </Collapsible>
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

/**
 * Assistant text as Markdown (GFM, no raw HTML, images as their alt text only), styled with theme
 * tokens. In a turn that called the
 * `retrieve` tool, `[n]` markers link to that call's structured sources (citations.ts), never to a
 * URL written in the text.
 */
export function MarkdownText({ text }: TextMessagePartProps) {
  const messageId = useAuiState((s) => s.message.id);
  const runId = messageRunId(messageId);
  const infos = useContext(ToolCallInfoContext);
  const sources = useMemo(() => (runId === null ? null : citationSourcesForRun(infos, runId)), [infos, runId]);
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
    <div
      className={cn(
        "min-w-0 space-y-2 break-words text-body leading-relaxed",
        "[&_a]:text-info-text [&_a]:underline [&_a]:underline-offset-2",
        "[&_code]:rounded-sm [&_code]:bg-fill-hover [&_code]:px-1 [&_code]:font-mono [&_code]:text-caption",
        "[&_pre]:overflow-x-auto [&_pre]:rounded-md [&_pre]:border [&_pre]:border-border [&_pre]:bg-fill-hover [&_pre]:p-3",
        "[&_pre_code]:bg-transparent [&_pre_code]:p-0",
        "[&_ul]:list-disc [&_ul]:pl-5 [&_ol]:list-decimal [&_ol]:pl-5",
        "[&_blockquote]:border-l-2 [&_blockquote]:border-border [&_blockquote]:pl-3 [&_blockquote]:text-muted-foreground",
        "[&_table]:block [&_table]:max-w-full [&_table]:overflow-x-auto [&_td]:border [&_td]:border-border [&_td]:px-2 [&_th]:border [&_th]:border-border [&_th]:px-2",
      )}
    >
      <SafeMarkdown citations={sources} components={components}>
        {text}
      </SafeMarkdown>
    </div>
  );
}
