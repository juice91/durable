"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { listEvents } from "@/lib/runs";
import {
  POST_TURN_POLL_MS,
  POST_TURN_TIMEOUT_MS,
  hasPostTurnSteps,
  memoryWrites,
  postTurnReported,
  recallSummary,
  type LooseRunEvent,
  type PostTurnFeatures,
  type RecallSummary,
} from "@/lib/memory";

/** The conversation's latest turn, as the chat follows it. */
export interface LatestTurn {
  runId: string;
  /** The run's display status (queued, running, completed, failed, …). */
  status: string;
  events: readonly LooseRunEvent[];
}

export interface MemoryActivity {
  /** The latest turn's recall: what the "Recall (last turn)" section shows. */
  recall: RecallSummary;
  /**
   * Bumps whenever a memory write is seen (a `memory_updated` or `memory_observe_failed` event, live
   * or found after the turn): the inspector reloads its notes and observations.
   */
  revision: number;
  /** True while a finished turn's post-turn memory steps have not reported yet ("Memory updating"). */
  updating: boolean;
}

const TERMINAL = new Set(["completed", "failed", "needs_attention"]);

/**
 * What the latest turn says about memory. The run stream ends at `run_completed`, but observational
 * memory and the recall index run after it (the RM.13 hand-off): when this page watched the turn
 * finish and the spec has those steps, the turn's events are read again every `pollMs` until they
 * report (`postTurnReported`) or `timeoutMs` passes. The observer writes nothing when there is not
 * enough to observe, so the timeout is the normal end of a quiet turn.
 */
export function useMemoryActivity(
  turn: LatestTurn | null,
  features: PostTurnFeatures,
  { pollMs = POST_TURN_POLL_MS, timeoutMs = POST_TURN_TIMEOUT_MS }: { pollMs?: number; timeoutMs?: number } = {},
): MemoryActivity {
  const [extra, setExtra] = useState<{ runId: string; events: LooseRunEvent[] } | null>(null);
  const [polling, setPolling] = useState<string | null>(null);
  // The status this hook last saw for each run: only a turn seen running is polled after it ends
  // (opening an old conversation never polls).
  const seenRef = useRef<{ runId: string; status: string } | null>(null);
  const runId = turn?.runId ?? null;
  const status = turn?.status ?? null;
  const { observational, semanticRecall } = features;
  // Read by the poller at each read: a feature toggled mid-poll changes what "reported" means, but
  // never restarts the poll or its deadline.
  const featuresRef = useRef<PostTurnFeatures>({ observational, semanticRecall });
  featuresRef.current = { observational, semanticRecall };
  // The poll's deadline, fixed when the turn is seen to complete (one per run).
  const deadlineRef = useRef<{ runId: string; at: number } | null>(null);

  useEffect(() => {
    if (!runId || !status) {
      seenRef.current = null;
      return;
    }
    const previous = seenRef.current;
    seenRef.current = { runId, status };
    const sawRunning = previous?.runId === runId && !TERMINAL.has(previous.status);
    if (sawRunning && status === "completed" && hasPostTurnSteps({ observational, semanticRecall })) {
      deadlineRef.current = { runId, at: Date.now() + timeoutMs };
      setPolling(runId);
    }
    // Only a status change starts a poll; the timeout is read then.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [runId, status, observational, semanticRecall]);

  // Another turn: drop what was read for the previous one.
  useEffect(() => {
    setExtra((current) => (current && current.runId !== runId ? null : current));
    setPolling((current) => (current && current !== runId ? null : current));
  }, [runId]);

  const baseEvents = turn?.events;
  const merged = useMemo<LooseRunEvent[]>(() => {
    const base = baseEvents ?? [];
    if (!extra || extra.runId !== runId) return [...base];
    const seen = new Set(base.map((item) => item.seq));
    return [...base, ...extra.events.filter((item) => !seen.has(item.seq))].sort((a, b) => a.seq - b.seq);
  }, [baseEvents, extra, runId]);

  const mergedRef = useRef(merged);
  mergedRef.current = merged;

  useEffect(() => {
    if (!polling) return;
    const pollRun = polling;
    if (!deadlineRef.current || deadlineRef.current.runId !== pollRun) deadlineRef.current = { runId: pollRun, at: Date.now() + timeoutMs };
    const deadline = deadlineRef.current.at;
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout> | null = null;
    const stopped = () => controller.signal.aborted;
    const finish = () => {
      controller.abort();
      setPolling((current) => (current === pollRun ? null : current));
    };
    // The read is cancelled the moment the poll stops (unmount, another turn, the end): the request
    // is aborted, and the race below also drops an answer that arrives anyway.
    const read = (lastSeq: number) =>
      new Promise<LooseRunEvent[]>((resolve, reject) => {
        controller.signal.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError")), { once: true });
        (listEvents(pollRun, lastSeq, controller.signal) as unknown as Promise<LooseRunEvent[]>).then(resolve, reject);
      });
    const schedule = () => {
      const left = deadline - Date.now();
      if (left <= 0) return finish();
      timer = setTimeout(() => void tick(), Math.min(pollMs, left));
    };
    const tick = async () => {
      if (stopped()) return;
      const known = mergedRef.current;
      if (postTurnReported(known, featuresRef.current)) return finish();
      // No read starts after the deadline: the last one is the read already in flight.
      if (Date.now() >= deadline) return finish();
      const lastSeq = known.reduce((max, item) => Math.max(max, item.seq), -1);
      try {
        const fresh = await read(lastSeq);
        if (stopped()) return;
        if (Array.isArray(fresh) && fresh.length > 0) {
          setExtra((current) => {
            const events = current && current.runId === pollRun ? current.events : [];
            const seen = new Set(events.map((item) => item.seq));
            return { runId: pollRun, events: [...events, ...fresh.filter((item) => !seen.has(item.seq))] };
          });
          if (postTurnReported([...known, ...fresh], featuresRef.current)) return finish();
        }
      } catch {
        if (stopped()) return;
        // A failed read: try again until the deadline.
      }
      schedule();
    };
    schedule();
    return () => {
      controller.abort();
      if (timer) clearTimeout(timer);
    };
    // The deadline and the features are read through refs: only a new poll restarts this.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [polling, pollMs]);

  return {
    recall: recallSummary(merged),
    revision: memoryWrites(merged),
    updating: polling !== null,
  };
}
