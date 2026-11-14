"use client";

import { useEffect, useRef, useState } from "react";
import { ingestProgress, type IngestProgress } from "@/lib/knowledge";
import { subscribeRun, type RunEvent } from "@/lib/runs";

/**
 * At most this many run streams are followed at once: each is a long-lived request, and a browser
 * allows about six per host over HTTP/1.1, which the rest of the page also needs. Runs past the cap
 * get their status from the Documents tab's poll instead, and a stream when one frees up.
 */
export const MAX_LIVE_INGEST_STREAMS = 3;

interface Live {
  stop: () => void;
  events: RunEvent[];
}

/**
 * Follows the given kb_ingest runs' streams (the first MAX_LIVE_INGEST_STREAMS of them) and returns
 * each one's latest `ingest_progress` (`{embedded, total}`). `onEnd(runId)` is called once when a
 * followed run ends (completed, failed, or gone), so the page can reload what it shows.
 */
export function useIngestProgress(runIds: readonly string[], onEnd: (runId: string) => void): Record<string, IngestProgress> {
  const [progress, setProgress] = useState<Record<string, IngestProgress>>({});
  const live = useRef(new Map<string, Live>());
  const ended = useRef(new Set<string>());
  const onEndRef = useRef(onEnd);
  onEndRef.current = onEnd;
  const key = runIds.join("|");

  useEffect(() => {
    const wanted = new Set(runIds.filter((id) => !ended.current.has(id)));
    // Stop streams of runs no longer listed.
    for (const [runId, entry] of live.current) {
      if (!wanted.has(runId)) {
        entry.stop();
        live.current.delete(runId);
      }
    }
    for (const runId of wanted) {
      if (live.current.size >= MAX_LIVE_INGEST_STREAMS) break;
      if (live.current.has(runId)) continue;
      const entry: Live = { stop: () => undefined, events: [] };
      live.current.set(runId, entry);
      try {
        entry.stop = subscribeRun(runId, {
          onEvent: (event) => {
            if ((event.type as string) !== "ingest_progress") return;
            entry.events.push(event);
            const latest = ingestProgress(entry.events);
            if (latest) setProgress((current) => ({ ...current, [runId]: latest }));
          },
          onEnd: () => {
            live.current.delete(runId);
            ended.current.add(runId);
            onEndRef.current(runId);
          },
        });
      } catch {
        // No credentials or no fetch: leave it to the poll.
        live.current.delete(runId);
      }
    }
    // `key` stands for runIds' content.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key]);

  useEffect(() => {
    const streams = live.current;
    return () => {
      for (const entry of streams.values()) entry.stop();
      streams.clear();
    };
  }, []);

  return progress;
}
