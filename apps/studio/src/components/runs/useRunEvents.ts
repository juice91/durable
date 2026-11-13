"use client";

import { useCallback, useEffect, useState } from "react";
import { ApiError, deltaReducer, getRun, listEvents, subscribeRun, type PartialTextMap, type Run, type RunEvent, type RunStatus } from "@/lib/runs";

function failedEventStatus(status: unknown): "failed" | "needs_attention" {
  return status === "needs_attention" ? "needs_attention" : "failed";
}

/** A running run whose heartbeat is older than this shows as recovering. */
export const RECOVERING_AFTER_MS = 20_000;

/** The run's status as shown: "recovering" while running with a stale heartbeat. */
export function runViewStatus(run: Run | null, now: number = Date.now()): RunStatus {
  if (!run) return "queued";
  if (run.display_status === "running" && run.heartbeat_at) {
    const heartbeatAge = now - new Date(run.heartbeat_at).getTime();
    if (heartbeatAge > RECOVERING_AFTER_MS) return "recovering";
  }
  return run.display_status;
}

export interface RunEventsState {
  run: Run | null;
  events: RunEvent[];
  partialText: PartialTextMap;
  /**
   * The last load's error: "Run not found" or "Failed to load run" while the run is unknown, or
   * "Failed to load the run's events" once the run is known (its events could not be read).
   */
  loadError: string | null;
  /** Load the run and its events again (Retry). */
  reload: () => void;
}

/**
 * One run's details and durable events (GET /runs/:id, GET /runs/:id/events), then the live stream:
 * new events by seq (no duplicates), partial text replaced by the durable model message
 * (durability rule 4), and status from status frames and terminal events.
 */
export function useRunEvents(runId: string): RunEventsState {
  const [run, setRun] = useState<Run | null>(null);
  const [events, setEvents] = useState<RunEvent[]>([]);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [partialText, setPartialText] = useState<PartialTextMap>({});
  // Bumped by reload(): loads again without clearing what is shown.
  const [attempt, setAttempt] = useState(0);
  const reload = useCallback(() => setAttempt((n) => n + 1), []);

  // A new run starts from nothing.
  useEffect(() => {
    setRun(null);
    setEvents([]);
    setLoadError(null);
    setPartialText({});
  }, [runId]);

  useEffect(() => {
    let cancelled = false;
    const load = async () => {
      let loaded = false;
      try {
        const runData = await getRun(runId);
        if (cancelled) return;
        loaded = runData !== null && runData !== undefined;
        setRun(loaded ? runData : null);
        const eventsData = await listEvents(runId);
        if (cancelled) return;
        // Merge by seq: live events that arrived meanwhile are kept.
        const fetched = Array.isArray(eventsData) ? eventsData : [];
        setEvents((prev) => {
          const bySeq = new Map(prev.map((e) => [e.seq, e]));
          for (const e of fetched) bySeq.set(e.seq, e);
          return [...bySeq.values()].sort((a, b) => a.seq - b.seq);
        });
        setLoadError(null);
      } catch (err: unknown) {
        if (cancelled) return;
        if (loaded) setLoadError("Failed to load the run's events");
        else setLoadError(err instanceof ApiError && err.status === 404 ? "Run not found" : "Failed to load run");
      }
    };
    void load();
    return () => {
      cancelled = true;
    };
  }, [runId, attempt]);

  // Subscribe once the run has loaded; status frames update `run`, so depend only on its presence
  // (depending on `run` itself would tear the stream down and replay it on every status frame).
  const runLoaded = run !== null;
  useEffect(() => {
    if (!runLoaded) return;
    const unsubscribe = subscribeRun(runId, {
      onEvent: (event) => {
        setEvents((prev) => {
          if (prev.some((e) => e.seq === event.seq)) return prev;
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

  return { run, events, loadError, partialText, reload };
}
