"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { getRun } from "@/lib/runs";
import { STUCK_PENDING_AFTER_MS, ingestStuckReason, type IngestRunState, type IngestStuckReason, type KnowledgeDocument } from "@/lib/knowledge";

/** How often pending documents are looked at again (their age, and a run that is still queued). */
export const STUCK_CHECK_MS = 15_000;
/**
 * At most this many pending documents have their run read per check: the first ones loaded, in list
 * order. A bulk upload with more pending
 * documents gets no Retry past the cap until earlier ones settle; the reingest route still works for
 * them, and the Runs tab shows their runs. The cap keeps a large upload from firing one GET per
 * document every check.
 */
export const STUCK_CHECK_LIMIT = 20;

const TERMINAL = new Set(["completed", "failed", "needs_attention"]);

/**
 * The keys of loaded `pending` documents whose ingest never started (`ingestStuck`): the Documents
 * tab offers Retry for them. Each pending document's `last_run_id` run is read once when it is
 * listed (an enqueue failure is already `failed` by then), and a run still `queued` is read again
 * every STUCK_CHECK_MS once the document is STUCK_PENDING_AFTER_MS old. A failed read leaves the
 * document as it is (no Retry), to be tried at the next check.
 */
export interface StuckIngests {
  /** Document key → why it is stuck, for the documents to offer Retry for. */
  stuck: ReadonlyMap<string, IngestStuckReason>;
  /** Forget what was read of a run (after a Retry handed it off again): it is read afresh. */
  recheck: (runId: string) => void;
}

export function useStuckIngests(documents: readonly KnowledgeDocument[]): StuckIngests {
  const [runs, setRuns] = useState<Record<string, IngestRunState>>({});
  const [now, setNow] = useState(() => Date.now());
  const fetchedAt = useRef(new Map<string, number>());
  const pending = useMemo(() => documents.filter((doc) => doc.status === "pending").slice(0, STUCK_CHECK_LIMIT), [documents]);
  const pendingKey = pending.map((doc) => `${doc.document_key}:${doc.last_run_id ?? ""}:${doc.updated_at}`).join("\n");

  // Re-evaluate the ages while anything is pending.
  useEffect(() => {
    if (pending.length === 0) return;
    const timer = setInterval(() => setNow(Date.now()), STUCK_CHECK_MS);
    return () => clearInterval(timer);
  }, [pending.length]);

  const runsRef = useRef(runs);
  runsRef.current = runs;
  // A read outlives a re-run of the effect below (a tick, a new list): its answer still applies,
  // unless the tab is gone.
  const mounted = useRef(true);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);
  useEffect(() => {
    const at = Date.now();
    for (const doc of pending) {
      const runId = doc.last_run_id;
      if (!runId) continue;
      const known = runsRef.current[runId];
      const last = fetchedAt.current.get(runId);
      const recheck =
        known !== undefined &&
        known !== null &&
        !TERMINAL.has(known.status) &&
        Date.parse(doc.updated_at) + STUCK_PENDING_AFTER_MS < at &&
        (last === undefined || at - last >= STUCK_CHECK_MS);
      if (last !== undefined && !recheck) continue;
      fetchedAt.current.set(runId, at);
      getRun(runId).then(
        (run) => {
          if (mounted.current) setRuns((current) => ({ ...current, [runId]: { status: run.status, created_at: run.created_at } }));
        },
        (reason: unknown) => {
          if (!mounted.current) return;
          // lib/runs throws its own ApiError: read the status, not the class.
          if ((reason as { status?: unknown } | null)?.status === 404) setRuns((current) => ({ ...current, [runId]: null }));
          // Any other failure: try again at the next check.
          else fetchedAt.current.delete(runId);
        },
      );
    }
    // `pendingKey` stands for `pending`'s content.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pendingKey, now]);

  const recheck = useCallback((runId: string) => {
    fetchedAt.current.delete(runId);
    setRuns((current) => {
      if (!(runId in current)) return current;
      const next = { ...current };
      delete next[runId];
      return next;
    });
    setNow(Date.now());
  }, []);

  const stuck = useMemo(() => {
    const out = new Map<string, IngestStuckReason>();
    for (const doc of pending) {
      const run: IngestRunState = doc.last_run_id ? runs[doc.last_run_id] : null;
      const reason = ingestStuckReason(doc, run, now);
      if (reason) out.set(doc.document_key, reason);
    }
    return out;
  }, [pending, runs, now]);
  return { stuck, recheck };
}
