import React from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { type RunEvent, type RunDelta, type PartialTextMap } from "@/lib/runs";

interface EventItemProps {
  event: RunEvent;
}

interface EventTimelineProps {
  events: RunEvent[];
  currentPartialText: PartialTextMap;
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

export function EventTimeline({ events, currentPartialText }: EventTimelineProps) {
  if (events.length === 0) {
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

function EventItem({ event }: EventItemProps) {
  const renderContent = () => {
    switch (event.type) {
      case "run_started":
        return (
          <div className="text-sm text-card-foreground/70">
            Run started
            {event.data.input && (
              <span className="block mt-1 text-xs font-mono text-muted-foreground">
                Input: {event.data.input}
              </span>
            )}
          </div>
        );
      case "model_message":
        return (
          <div className="text-sm text-card-foreground">
            <span className="font-semibold">Model response:</span>
            <div className="mt-1">
              <MarkdownContent text={event.data.text} />
            </div>
          </div>
        );
      case "tool_call":
        return (
          <div className="text-sm">
            <span className="font-semibold text-purple-700 dark:text-purple-400">Tool call:</span>
            <span className="text-purple-600 dark:text-purple-300 ml-1">
              {event.data.tool_name}
            </span>
            <pre className="mt-1 text-xs text-muted-foreground bg-muted p-2 rounded">
              {JSON.stringify(event.data.args, null, 2)}
            </pre>
          </div>
        );
      case "tool_result":
        return (
          <div className="text-sm">
            <span className="font-semibold text-green-700 dark:text-green-400">Tool result:</span>
            <pre className="mt-1 text-xs text-muted-foreground bg-muted p-2 rounded">
              {JSON.stringify(event.data.content, null, 2)}
            </pre>
          </div>
        );
      case "run_completed":
        return (
          <div className="text-sm text-green-700 dark:text-green-400 font-semibold">
            Run completed successfully
          </div>
        );
      case "run_failed":
        return (
          <div className="text-sm text-red-700 dark:text-red-400 font-semibold">
            Run failed
            <span className="block mt-1 text-red-600 dark:text-red-300">
              {event.data.error || "Unknown error"}
            </span>
          </div>
        );
      default:
        return <div className="text-sm text-muted-foreground">{JSON.stringify(event.data)}</div>;
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
