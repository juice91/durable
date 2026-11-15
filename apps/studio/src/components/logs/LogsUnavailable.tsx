import React from "react";
import type { LogsError } from "./logsQuery";

/** A failed search. "unavailable" (HTTP 503) is the Loki-down / not-configured / busy state: logs
 * are optional infrastructure, so the copy says what still works and offers a retry. */
export function LogsUnavailable({
  error,
  onRetry,
  onClearAgent,
  compact = false,
}: {
  error: LogsError;
  onRetry?: () => void;
  /** For an unknown agent (`agent_not_found`): clear the agent filter. */
  onClearAgent?: () => void;
  compact?: boolean;
}) {
  const unavailable = error.kind === "unavailable";
  return (
    <div
      role={unavailable ? "status" : "alert"}
      data-testid={unavailable ? "logs-unavailable" : "logs-error"}
      className={`rounded-md border border-border bg-muted/50 text-sm ${compact ? "p-3" : "p-6 text-center"}`}
    >
      <p className="font-medium text-card-foreground">{unavailable ? "Loki unavailable" : "Could not load logs"}</p>
      <p className="mt-1 text-muted-foreground">{error.message}.</p>
      {unavailable ? (
        <p className="mt-1 text-muted-foreground">
          Log search needs Loki (the LGTM stack). Runs, traces and metrics still work without it.
        </p>
      ) : null}
      {error.kind === "agent_not_found" && onClearAgent ? (
        <button
          type="button"
          onClick={onClearAgent}
          data-testid="logs-clear-agent"
          className="mr-2 mt-3 inline-flex items-center rounded-md bg-muted px-3 py-1.5 text-xs font-medium text-card-foreground hover:bg-muted/80 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        >
          Clear agent filter
        </button>
      ) : null}
      {onRetry && error.kind !== "agent_not_found" ? (
        <button
          type="button"
          onClick={onRetry}
          className="mt-3 inline-flex items-center rounded-md bg-muted px-3 py-1.5 text-xs font-medium text-card-foreground hover:bg-muted/80 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        >
          Retry
        </button>
      ) : null}
    </div>
  );
}
