"use client";

import React, { useState, useEffect } from "react";
import { useRouter, useParams } from "next/navigation";
import { ApiError, getRun, listEvents, subscribeRun, type Run, type RunEvent, deltaReducer, type RunStatus } from "@/lib/runs";
import { StatusPill } from "@/components/runs/StatusPill";
import { EventTimeline } from "@/components/runs/EventTimeline";
import { buildTraceLink, buildLogsLink } from "@/lib/grafana";

function failedEventStatus(status: unknown): "failed" | "needs_attention" {
  return status === "needs_attention" ? "needs_attention" : "failed";
}

export default function RunDetailPage() {
  const [run, setRun] = useState<Run | null>(null);
  const [events, setEvents] = useState<RunEvent[]>([]);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [partialText, setPartialText] = useState<Record<string, { text: string; attempt: string }>>({});
  const router = useRouter();
  const params = useParams();

  const runId = typeof params.id === "string" ? params.id : undefined;
  const hasTrace = run?.trace_id !== null && run?.trace_id !== undefined;

  useEffect(() => {
    if (!runId) return;

    const loadRun = async () => {
      try {
        const runData = await getRun(runId);
        setRun(runData);

        const eventsData = await listEvents(runId);
        setEvents(eventsData);
        setLoadError(null);
      } catch (err: unknown) {
        if (err instanceof ApiError && err.status === 404) {
          setLoadError("Run not found");
        } else {
          setLoadError("Failed to load run");
        }
      }
    };

    loadRun();
  }, [runId]);

  // Subscribe once the run has loaded; status frames update `run`, so depend only on its presence
  // (depending on `run` itself would tear the stream down and replay it on every status frame).
  const runLoaded = run !== null;
  useEffect(() => {
    if (!runId || !runLoaded) return;

    const unsubscribe = subscribeRun(runId, {
      onEvent: (event) => {
        setEvents((prev) => {
          if (prev.some((e) => e.seq === event.seq)) {
            return prev;
          }
          return [...prev, event].sort((a, b) => a.seq - b.seq);
        });
        // Durability rule 4: the durable model message replaces its partial text.
        if (event.type === "model_message") {
          setPartialText((prev) => deltaReducer(prev, { type: "model_message", payload: event }));
        } else if (event.type === "run_completed" || event.type === "run_failed") {
          setPartialText((prev) => deltaReducer(prev, { type: event.type === "run_completed" ? "done" : "failed" }));
          setRun((prev) => {
            if (!prev) return null;
            if (event.type === "run_completed") {
              return {
                ...prev,
                status: "completed",
                display_status: "completed",
                output: typeof event.data.output === "string" ? event.data.output : prev.output,
                error: null,
              };
            }
            const terminalStatus = failedEventStatus(event.data.status);
            return {
              ...prev,
              status: terminalStatus,
              display_status: terminalStatus,
              error: typeof event.data.error === "string" ? event.data.error : prev.error,
            };
          });
        }
      },
      onDelta: (delta) => {
        setPartialText((prev) => deltaReducer(prev, { type: "delta", payload: delta }));
      },
      onStatus: (status) => {
        setRun((prev) => (prev ? { ...prev, display_status: status.display_status } : null));
      },
    });

    return unsubscribe;
  }, [runId, runLoaded]);

  const getStatus = () => {
    if (!run) return "queued";
    // Check for recovery status based on heartbeat
    if (run.display_status === "running" && run.heartbeat_at) {
      const heartbeatAge = Date.now() - new Date(run.heartbeat_at).getTime();
      if (heartbeatAge > 20000) { // 20 seconds
        return "recovering";
      }
    }
    return run.display_status;
  };

  const status = getStatus();

  if (loadError && !run) {
    return (
      <div className="flex items-center justify-center py-12">
        <p className="text-destructive-foreground">{loadError}</p>
      </div>
    );
  }

  if (!run) {
    return (
      <div className="flex items-center justify-center py-12">
        <p className="text-card-foreground/60">Loading run...</p>
      </div>
    );
  }

  return (
    <div className="max-w-4xl mx-auto">
        <div className="mb-6">
          <button
            onClick={() => router.push("/agents")}
            className="text-sm text-card-foreground/80 hover:text-card-foreground mb-4 inline-flex items-center"
          >
            ← Back to agents
          </button>
          <div className="flex items-center justify-between">
            <div>
              <h1 className="text-2xl font-bold text-card-foreground">Run {run.run_id}</h1>
              <p className="text-sm text-muted-foreground mt-1">
                {run.kind === "tool"
                  ? `Tool: ${run.tool_id ?? "unknown"}`
                  : `Agent: ${run.agent_id} | Version: ${run.version ?? "latest"}`}
              </p>
            </div>
            <div className="flex items-center gap-2">
              {hasTrace && (
                <div className="flex items-center gap-2">
                  <a
                    href={buildTraceLink(run.trace_id!)}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="inline-flex items-center rounded-md bg-muted px-3 py-1.5 text-xs font-medium text-muted-foreground hover:bg-muted/80"
                  >
                    Trace
                  </a>
                  <a
                    href={buildLogsLink(run.trace_id!)}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="inline-flex items-center rounded-md bg-muted px-3 py-1.5 text-xs font-medium text-muted-foreground hover:bg-muted/80"
                  >
                    Logs
                  </a>
                </div>
              )}
              <StatusPill status={status} showRecoveringNotice={status === "recovering"} />
            </div>
          </div>

          {status === "recovering" && (
            <div className="mt-4 p-3 bg-muted border border-border rounded-md text-muted-foreground text-sm">
              ⚠️ The worker is restarting. Events may be delayed as the system recovers.
            </div>
          )}
          {(status === "failed" || status === "needs_attention") && (
            <div role="alert" className="mt-4 rounded-md border border-red-200 bg-red-50 p-3 text-sm text-red-700">
              {run.error ||
                (status === "needs_attention"
                  ? "This run needs attention before it can continue."
                  : "The run failed.")}
            </div>
          )}
        </div>

        <div className="bg-card rounded-lg shadow p-6">
          <h2 className="text-lg font-semibold mb-4 text-card-foreground">Event Timeline</h2>
          <EventTimeline events={events} currentPartialText={partialText} />
        </div>
    </div>
  );
}
