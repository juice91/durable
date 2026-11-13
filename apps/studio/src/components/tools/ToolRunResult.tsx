"use client";

import React, { useEffect, useState } from "react";
import { Check, Copy } from "lucide-react";
import { Button } from "@/components/ui/Button";
import { ICON_STROKE_WIDTH } from "@/lib/icons";
import { formatDuration } from "@/lib/traces";
import Link from "next/link";
import { StatusPill } from "@/components/runs/StatusPill";
import { getRun, subscribeRun, type Run, type RunEvent, type RunStatus } from "@/lib/runs";
import { describeToolRun } from "@/lib/toolRuns";
import { routes } from "@/lib/routes";

/** created_at → updated_at of a finished run, in ms; null while running or when a time is missing. */
export function toolRunDurationMs(run: Pick<Run, "created_at" | "updated_at"> | null): number | null {
  if (!run) return null;
  const start = Date.parse(run.created_at);
  const end = Date.parse(run.updated_at);
  if (!Number.isFinite(start) || !Number.isFinite(end) || end < start) return null;
  return end - start;
}

interface ToolRunResultProps {
  runId: string;
  initialStatus: RunStatus;
}

/**
 * Follows one tool run over the run stream until it ends, then loads the run for its output and
 * error. Never retries the tool: a needs_attention run is shown as such and left to the user.
 */
export function ToolRunResult({ runId, initialStatus }: ToolRunResultProps) {
  const [status, setStatus] = useState<RunStatus>(initialStatus);
  const [run, setRun] = useState<Run | null>(null);
  const [terminalEvent, setTerminalEvent] = useState<RunEvent | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);

  useEffect(() => {
    let cancelled = false;
    setStatus(initialStatus);
    setRun(null);
    setTerminalEvent(null);
    setLoadError(null);

    const loadFinal = () => {
      getRun(runId)
        .then((loaded) => {
          if (cancelled) return;
          setRun(loaded);
          setStatus(loaded.display_status);
        })
        .catch((error: unknown) => {
          if (!cancelled) setLoadError(error instanceof Error ? error.message : "Failed to load the run");
        });
    };

    let unsubscribe: () => void = () => undefined;
    try {
      unsubscribe = subscribeRun(runId, {
        onStatus: (frame) => {
          if (!cancelled && frame.display_status) setStatus(frame.display_status);
        },
        onEvent: (event) => {
          if (!cancelled && (event.type === "run_completed" || event.type === "run_failed")) setTerminalEvent(event);
        },
        onEnd: (reason) => {
          if (cancelled) return;
          if (reason === "not_found") setLoadError("Run not found");
          else loadFinal();
        },
      });
    } catch (error) {
      setLoadError(error instanceof Error ? error.message : "Failed to follow the run");
    }
    return () => {
      cancelled = true;
      unsubscribe();
    };
  }, [runId, initialStatus]);

  const view = describeToolRun(status, run, terminalEvent);
  // The run row is loaded once the stream ends, so a duration only shows for a finished run.
  const durationMs = toolRunDurationMs(run);

  const copyOutput = async () => {
    if (view.result === null) return;
    try {
      await navigator.clipboard.writeText(view.result);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      setCopied(false);
    }
  };
  const toneClass = {
    pending: "border-border bg-muted/40",
    success: "border-primary/30 bg-primary/5",
    error: "border-destructive/30 bg-destructive/10",
    attention: "border-destructive/50 bg-destructive/10",
  }[view.tone];

  return (
    <section data-testid="tool-run-result" data-status={view.status} aria-label="Tool run result" className="min-w-0 rounded-lg border border-border bg-card p-4">
      <div className="mb-3 flex flex-wrap items-center justify-between gap-3">
        <div className="flex items-center gap-3">
          <h3 className="font-semibold text-card-foreground">Result</h3>
          <StatusPill status={view.status} showRecoveringNotice />
          {durationMs !== null ? <span data-testid="tool-run-duration" className="text-caption tabular-nums text-muted-foreground">{formatDuration(durationMs)}</span> : null}
        </div>
        <Link href={routes.run(runId)} data-testid="tool-run-link" className="text-sm text-primary underline-offset-4 hover:underline">
          Open run {runId.slice(0, 8)}
        </Link>
      </div>
      <div className={`rounded-md border p-3 text-sm ${toneClass}`}>
        {view.tone === "pending" ? <p className="text-muted-foreground" aria-live="polite">{view.title}…</p> : null}
        {view.tone === "attention" ? (
          <div role="alert" data-testid="tool-run-needs-attention" className="space-y-1 text-destructive-text">
            <p className="font-semibold">{view.title}</p>
            <p className="text-xs">It was not retried automatically. Open the run and check the target system before running it again.</p>
          </div>
        ) : null}
        {view.tone === "error" ? <p role="alert" className="font-semibold text-destructive-text">{view.title}</p> : null}
        {view.error ? <p data-testid="tool-run-error" className="mt-1 whitespace-pre-wrap text-destructive-text">{view.error}</p> : null}
        {view.errorCode ? <p className="mt-1 text-xs text-muted-foreground">Error code: <code data-testid="tool-run-error-code" className="font-mono">{view.errorCode}</code></p> : null}
        {view.tone === "success" ? (
          view.result !== null
            ? (
              <div className="space-y-2">
                <div className="flex justify-end">
                  <Button size="sm" variant="ghost" onClick={() => void copyOutput()} data-testid="tool-run-copy">
                    {copied ? <Check strokeWidth={ICON_STROKE_WIDTH} aria-hidden="true" /> : <Copy strokeWidth={ICON_STROKE_WIDTH} aria-hidden="true" />}
                    {copied ? "Copied" : "Copy"}
                  </Button>
                </div>
                <pre data-testid="tool-run-output" className="max-h-96 overflow-auto rounded-md bg-muted p-3 font-mono text-xs text-foreground">{view.result}</pre>
              </div>
            )
            : <p className="text-muted-foreground">Completed with no output.</p>
        ) : null}
      </div>
      {loadError ? <p role="alert" className="mt-2 text-xs text-destructive-text">{loadError}</p> : null}
    </section>
  );
}
