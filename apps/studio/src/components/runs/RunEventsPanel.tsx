"use client";

import React, { useId } from "react";
import { buildLogsLink, buildTraceLink } from "@/lib/grafana";
import { Button } from "@/components/ui/Button";
import { EventTimeline } from "./EventTimeline";
import { useKnowledgeBaseNames } from "@/components/knowledge/useKnowledgeBaseNames";
import { RunSubject } from "./RunSubject";
import { StatusPill } from "./StatusPill";
import { runViewStatus, useRunEvents } from "./useRunEvents";

const chip = "inline-flex items-center rounded-md bg-muted px-3 py-1.5 text-xs font-medium text-muted-foreground hover:bg-muted/80";

/** The run's events could not be read: the message and Retry. */
function EventsError({ message, onRetry, className = "" }: { message: string; onRetry: () => void; className?: string }) {
  return (
    <div role="alert" data-testid="run-events-error" className={`flex flex-wrap items-center justify-between gap-3 rounded-md border border-destructive/40 bg-destructive/10 p-3 text-sm text-destructive-text ${className}`}>
      <span className="min-w-0">{message}</span>
      <Button size="sm" variant="secondary" onClick={onRetry}>Retry</Button>
    </div>
  );
}

/**
 * A run's details and live event timeline (formerly the `/runs/:id` page, now the trace Sheet's
 * Events tab): what it ran, its live status, Grafana links, the recovering notice, the failure,
 * then the durable events and the live partial text.
 */
export function RunEventsPanel({ runId }: { runId: string }) {
  const { run, events, loadError, partialText, reload } = useRunEvents(runId);
  const knowledgeBaseNames = useKnowledgeBaseNames(run?.kind === "kb_ingest");
  const headingId = useId();

  if (loadError && !run) {
    return (
      <div className="flex flex-col items-center justify-center gap-3 py-12" data-testid="run-events-panel">
        <p role="alert" className="text-destructive-text" data-testid="run-events-error">{loadError}</p>
        <Button size="sm" variant="secondary" onClick={reload}>Retry</Button>
      </div>
    );
  }
  if (!run) {
    return (
      <div className="flex items-center justify-center py-12" data-testid="run-events-panel">
        <p className="text-muted-foreground">Loading run...</p>
      </div>
    );
  }

  const status = runViewStatus(run);
  const traceId = run.trace_id ?? null;
  return (
    <div className="flex min-w-0 flex-col gap-4" data-testid="run-events-panel">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <p className="min-w-0 text-sm text-muted-foreground">
          <RunSubject run={run} showVersion knowledgeBaseNames={knowledgeBaseNames} />
        </p>
        <div className="flex flex-wrap items-center gap-2">
          {traceId ? (
            <>
              <a href={buildTraceLink(traceId)} target="_blank" rel="noopener noreferrer" className={chip}>
                Trace
              </a>
              <a href={buildLogsLink(traceId)} target="_blank" rel="noopener noreferrer" className={chip}>
                Logs
              </a>
            </>
          ) : null}
          <StatusPill status={status} showRecoveringNotice={status === "recovering"} />
        </div>
      </div>

      {status === "recovering" ? (
        <div className="rounded-md border border-border bg-muted p-3 text-sm text-muted-foreground">
          The worker is restarting. Events may be delayed as the system recovers.
        </div>
      ) : null}
      {status === "failed" || status === "needs_attention" ? (
        <div role="alert" className="rounded-md border border-destructive/40 bg-destructive/10 p-3 text-sm text-destructive-text">
          {run.error || (status === "needs_attention" ? "This run needs attention before it can continue." : "The run failed.")}
        </div>
      ) : null}

      <section aria-labelledby={headingId} className="rounded-lg border border-border bg-card p-4">
        <h3 id={headingId} className="mb-4 text-sm font-semibold text-card-foreground">Event Timeline</h3>
        {loadError && events.length === 0 ? (
          // The events could not be read: say so (not "No events were recorded"), with Retry.
          <EventsError message={loadError} onRetry={reload} />
        ) : (
          <>
            {loadError ? (
              <EventsError message={loadError} onRetry={reload} className="mb-3" />
            ) : null}
            <EventTimeline events={events} currentPartialText={partialText} status={status} />
          </>
        )}
      </section>
    </div>
  );
}
