import React from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { formatMatchCounts } from "@/lib/processors";
import { type RunEvent, type RunDelta, type PartialTextMap, type RunStatus } from "@/lib/runs";

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
      <div className="flex-1">
        <div className="text-xs font-semibold text-card-foreground/60 mb-1 uppercase tracking-wide">
          {event.type}
        </div>
        {renderContent()}
      </div>
    </div>
  );
}
