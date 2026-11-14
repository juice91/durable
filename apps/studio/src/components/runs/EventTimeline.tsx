import React from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { formatMatchCounts } from "@/lib/processors";
import { type RunEvent, type RunDelta, type PartialTextMap, type RunStatus } from "@/lib/runs";
import { TraceLink } from "@/components/traces/TraceLink";

interface EventItemProps {
  event: RunEvent;
}

interface EventTimelineProps {
  events: RunEvent[];
  currentPartialText: PartialTextMap;
  /** The run's display status: a terminal run with no events gets an "ended" placeholder, not
   * "check back soon" (e.g. a superseded conversation turn that never started). */
  status?: RunStatus;
}

const TERMINAL_STATUSES: ReadonlySet<RunStatus> = new Set<RunStatus>(["completed", "failed", "needs_attention"]);

/** Event payload values are JSON: text stays as is, anything else is shown as pretty JSON (a tool
 * run's input and result are objects, which React cannot render as children). */
export function formatValue(value: unknown): string {
  if (typeof value === "string") return value;
  return JSON.stringify(value, null, 2) ?? String(value);
}

function JsonBlock({ value, label }: { value: unknown; label?: string }) {
  return (
    <pre aria-label={label} className="mt-1 text-xs text-muted-foreground bg-muted p-2 rounded whitespace-pre-wrap break-words">
      {formatValue(value)}
    </pre>
  );
}

function MarkdownContent({ text }: { text: string }) {
  return (
    <div className="prose dark:prose-invert max-w-none text-sm text-card-foreground/80">
      <ReactMarkdown remarkPlugins={[remarkGfm]} skipHtml>
        {text}
      </ReactMarkdown>
    </div>
  );
}

export function EventTimeline({ events, currentPartialText, status }: EventTimelineProps) {
  if (events.length === 0) {
    if (status !== undefined && TERMINAL_STATUSES.has(status)) {
      return (
        <div className="p-4 text-card-foreground bg-card rounded-lg text-center">
          No events were recorded. This run has ended.
        </div>
      );
    }
    return (
      <div className="p-4 text-card-foreground bg-card rounded-lg text-center">
        No events yet. Run started, check back soon.
      </div>
    );
  }

  return (
    <div className="space-y-4">
      {events.map((event) => (
        <EventItem key={event.seq} event={event} />
      ))}

      {/* Show current partial text under active model request */}
      {Object.entries(currentPartialText).length > 0 && (
        <div className="mt-4 p-4 bg-card border border-border rounded-lg">
          <h4 className="text-sm font-semibold text-card-foreground mb-2">Live partial text</h4>
          <div className="space-y-2">
            {Object.entries(currentPartialText).map(([modelRequest, partial]) => (
              <div key={modelRequest} className="text-sm text-card-foreground/80">
                <span className="font-mono text-xs text-muted-foreground mr-2">
                  Request #{modelRequest}
                </span>
                <span>{partial.text}</span>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

/** One processor outcome as a compact line: phase, processor type, action, then the match counts,
 * flagged categories or reason. Only these fields are read; the event never carries text, and none
 * is shown even if it did. */
function ProcessorLine({ data }: { data: RunEvent["data"] }) {
  const phase = data.phase === "input" ? "Input" : data.phase === "output" ? "Output" : "";
  const type = typeof data.processor_type === "string" ? data.processor_type : "processor";
  const action = typeof data.action === "string" ? data.action : "";
  const counts = formatMatchCounts(data.match_counts, "");
  const categories = Array.isArray(data.categories)
    ? data.categories.filter((c): c is string => typeof c === "string").join(", ")
    : "";
  const reason = typeof data.reason === "string" ? data.reason : "";
  const detail = [counts, categories && `flagged: ${categories}`, reason && `reason: ${reason}`]
    .filter(Boolean)
    .join("; ");
  const blocked = action === "block";
  return (
    <div className="text-sm" data-testid="processor-result">
      <span className="font-semibold">{`${phase} processor`.trim()}</span>
      <span className="ml-1 font-mono">{type}</span>
      {action && (
        <span className={blocked ? "ml-1 font-semibold text-destructive-text" : "ml-1 text-muted-foreground"}>
          {action}
        </span>
      )}
      {typeof data.model_request === "number" && (
        <span className="ml-1 text-xs text-muted-foreground">(request #{data.model_request})</span>
      )}
      {detail && <span className="block mt-1 text-xs text-muted-foreground">{detail}</span>}
    </div>
  );
}

// ------------------------------------------------------------------ RAG and memory rows (RM.19)
//
// These events carry ids, counts, model and cost only (never memory, chunk or query text); the rows
// read an allowlist of fields, so nothing else is shown even if an event carried it.

type Fact = [label: string, value: string | null];

function factNumber(value: unknown): string | null {
  return typeof value === "number" && Number.isFinite(value) ? value.toLocaleString("en-US") : null;
}

function factText(value: unknown): string | null {
  return typeof value === "string" && value !== "" ? value : null;
}

function factCost(value: unknown): string | null {
  const text = factText(value);
  if (text === null) return null;
  const n = Number(text);
  return Number.isFinite(n) ? `$${n < 0.01 && n > 0 ? n.toPrecision(2) : n.toFixed(4)}` : null;
}

function Facts({ title, facts, tone = "normal", testId }: { title: string; facts: Fact[]; tone?: "normal" | "error"; testId: string }) {
  const shown = facts.filter((f): f is [string, string] => f[1] !== null);
  return (
    <div className="text-sm" data-testid={testId}>
      <span className={tone === "error" ? "font-semibold text-destructive-text" : "font-semibold text-card-foreground"}>{title}</span>
      {shown.length > 0 && (
        <span className="mt-1 block break-words text-xs text-muted-foreground">
          {shown.map(([label, value]) => `${label}: ${value}`).join(" · ")}
        </span>
      )}
    </div>
  );
}

const EMBEDDING_STAGE_TITLES: Record<string, string> = { recall: "Semantic recall", index: "Memory index" };

function EmbeddingLine({ data }: { data: RunEvent["data"] }) {
  const stage = factText(data.stage);
  const failed = data.status === "failed";
  const title = `${(stage && EMBEDDING_STAGE_TITLES[stage]) || "Embedding"}${failed ? " failed" : ""}`;
  return (
    <Facts
      testId="event-embedding"
      title={title}
      tone={failed ? "error" : "normal"}
      facts={[
        ["error", factText(data.error_code)],
        ["model", factText(data.model_id)],
        ["dimensions", factNumber(data.dimensions)],
        ["inputs", factNumber(data.inputs)],
        ["tokens", failed ? null : factNumber(data.input_tokens)],
        ["cost", factCost(data.cost_usd)],
        ["hits", factNumber(data.hits)],
        ["turns", factNumber(data.turns)],
        ["messages", factNumber(data.messages)],
        ["inserted", factNumber(data.inserted)],
        ["already indexed", factNumber(data.existing)],
        ["redacted", factNumber(data.redacted)],
      ]}
    />
  );
}

const INGEST_PHASE_TITLES: Record<string, string> = {
  chunked: "Document chunked",
  embedding: "Embedding batch",
  finalized: "Ingest finalized",
};

function IngestLine({ data }: { data: RunEvent["data"] }) {
  const phase = factText(data.phase);
  const progress =
    factNumber(data.embedded) !== null && factNumber(data.total) !== null
      ? `${factNumber(data.embedded)} of ${factNumber(data.total)} sections`
      : null;
  return (
    <Facts
      testId="event-ingest-progress"
      title={(phase && INGEST_PHASE_TITLES[phase]) || "Ingest progress"}
      facts={[
        ["batch", factNumber(data.batch)],
        ["embedded", progress],
        ["chunks", factNumber(data.chunks)],
        ["model", factText(data.model_id)],
        ["tokens", factNumber(data.input_tokens)],
        ["tokens to embed", factNumber(data.pending_tokens)],
        ["cost", factCost(data.cost_usd)],
        ["generation", factNumber(data.generation)],
      ]}
    />
  );
}

function MemoryLine({ data, failed }: { data: RunEvent["data"]; failed: boolean }) {
  const stage = factText(data.stage);
  const usage = data.usage && typeof data.usage === "object" ? (data.usage as Record<string, unknown>) : null;
  const covers =
    factNumber(data.covers_from_turn) !== null && factNumber(data.covers_to_turn) !== null
      ? `${factNumber(data.covers_from_turn)} to ${factNumber(data.covers_to_turn)}`
      : null;
  if (stage === "observe" || stage === "reflect") {
    const who = stage === "reflect" ? "Reflector" : "Observer";
    return (
      <Facts
        testId="event-memory-observer"
        title={failed ? `${who} failed` : stage === "reflect" ? "Observations reflected" : "Observations added"}
        tone={failed ? "error" : "normal"}
        facts={[
          ["error", factText(data.error_code)],
          ["turns", covers],
          ["notes", factNumber(data.notes)],
          ["approx. tokens", factNumber(data.approx_tokens)],
          ["superseded", factNumber(data.superseded)],
          ["redacted", factNumber(data.redacted)],
          ["model", factText(data.model_id)],
          ["tokens in", usage ? factNumber(usage.input_tokens) : null],
          ["tokens out", usage ? factNumber(usage.output_tokens) : null],
          ["cost", factCost(data.cost_usd)],
        ]}
      />
    );
  }
  const keys = Array.isArray(data.changed_keys) ? data.changed_keys.filter((k): k is string => typeof k === "string") : [];
  return (
    <Facts
      testId="event-memory-working"
      title={failed ? "Memory update failed" : "Working memory updated"}
      tone={failed ? "error" : "normal"}
      facts={[
        ["error", factText(data.error_code)],
        ["scope", factText(data.scope)],
        ["version", factNumber(data.version)],
        ["changed", keys.length ? keys.join(", ") : null],
        ["redacted", factNumber(data.redacted)],
      ]}
    />
  );
}

function EventItem({ event }: EventItemProps) {
  const renderContent = () => {
    switch (event.type) {
      case "run_started":
        return (
          <div className="text-sm text-card-foreground/70">
            Run started
            {typeof event.data.tool_id === "string" && (
              <span className="block mt-1 text-xs font-mono text-muted-foreground">
                Tool: {event.data.tool_id}
              </span>
            )}
            {typeof event.data.input === "string"
              ? event.data.input && (
                  <span className="block mt-1 text-xs font-mono text-muted-foreground">
                    Input: {event.data.input}
                  </span>
                )
              : event.data.input !== undefined &&
                event.data.input !== null && (
                  <div className="mt-1 text-xs text-muted-foreground">
                    Input:
                    <JsonBlock value={event.data.input} label="Run input" />
                  </div>
                )}
          </div>
        );
      case "model_message":
        return (
          <div className="text-sm text-card-foreground">
            <span className="font-semibold">Model response:</span>
            <div className="mt-1">
              <MarkdownContent text={event.data.text ?? ""} />
            </div>
          </div>
        );
      case "tool_call":
        return (
          <div className="text-sm">
            <span className="font-semibold text-primary">Tool call:</span>
            <span className="ml-1 font-mono text-muted-foreground">
              {formatValue(event.data.tool_id ?? event.data.tool_name)}
            </span>
            <JsonBlock value={event.data.args ?? {}} label="Tool arguments" />
          </div>
        );
      case "tool_result":
        return (
          <div className="text-sm">
            {event.data.status === "error" ? (
              <>
                <span className="font-semibold text-destructive-text">Tool error:</span>
                {event.data.content !== undefined ? (
                  <JsonBlock value={event.data.content} label="Tool result" />
                ) : (
                  <span className="block mt-1 text-destructive-text">
                    {formatValue(event.data.error ?? "Unknown error")}
                  </span>
                )}
              </>
            ) : (
              <>
                <span className="font-semibold text-success">Tool result:</span>
                <JsonBlock value={event.data.content ?? null} label="Tool result" />
              </>
            )}
          </div>
        );
      case "run_completed":
        return (
          <div className="text-sm">
            <span className="text-success font-semibold">Run completed successfully</span>
            {/* A tool run's end event carries the tool result (any JSON value) */}
            {event.data.result !== undefined && <JsonBlock value={event.data.result} label="Run result" />}
          </div>
        );
      case "run_failed":
        return (
          <div className="text-sm text-destructive-text font-semibold">
            {event.data.status === "needs_attention" ? "Needs attention" : "Run failed"}
            <span className="block mt-1 text-destructive-text">
              {formatValue(event.data.error || "Unknown error")}
            </span>
          </div>
        );
      case "processor_result":
        return <ProcessorLine data={event.data} />;
      case "embedding":
        return <EmbeddingLine data={event.data} />;
      case "ingest_progress":
        return <IngestLine data={event.data} />;
      case "memory_updated":
        return <MemoryLine data={event.data} failed={false} />;
      case "memory_observe_failed":
        return <MemoryLine data={event.data} failed />;
      default:
        return <JsonBlock value={event.data} />;
    }
  };

  return (
    <div className="flex gap-3">
      <div className="flex-shrink-0 w-12 pt-1">
        <div className="text-xs text-muted-foreground font-mono">
          seq {event.seq}
        </div>
        <div className="text-xs text-muted-foreground/70 mt-0.5">
          {new Date(event.ts).toLocaleTimeString()}
        </div>
      </div>
      <div className="min-w-0 flex-1">
        <div className="text-xs font-semibold text-card-foreground/60 mb-1 uppercase tracking-wide">
          {event.type}
        </div>
        {renderContent()}
        {/* A workflow step's child run: its trace stacks over this one where the trace Sheet is hosted. */}
        {typeof event.data.child_run_id === "string" && event.data.child_run_id ? (
          <TraceLink
            runId={event.data.child_run_id}
            data-testid="event-child-run-link"
            className="mt-1 inline-block text-xs text-primary underline-offset-4 hover:underline"
          >
            Open child run trace
          </TraceLink>
        ) : null}
      </div>
    </div>
  );
}
