"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { withTimeout } from "@/lib/agentUsage";
import { ApiError } from "@/lib/api";
import { getDocument, isIngestInFlight, listDocuments, type KnowledgeDocument } from "@/lib/knowledge";

/** Documents fetched per page (the API allows 1–200). */
export const DOCUMENT_PAGE_SIZE = 50;
/** While any loaded document is pending or ingesting, its row is re-read this often. */
export const DOCUMENT_POLL_MS = 3_000;
/** After a failed poll the next one waits twice as long, up to this. */
export const DOCUMENT_POLL_MAX_MS = 30_000;
/** A poll's read is abandoned (aborted) after this long, so one stuck request cannot stall polling. */
export const DOCUMENT_POLL_TIMEOUT_MS = 10_000;
/** At most this many in-flight documents are re-read per poll. */
export const DOCUMENT_POLL_LIMIT = 20;

export interface KnowledgeDocumentsState {
  documents: KnowledgeDocument[];
  loading: boolean;
  loadingMore: boolean;
  error: string | null;
  /** More pages exist (`next_after`). */
  hasMore: boolean;
  reload: () => void;
  loadMore: () => void;
  /** Add or replace a document (after an upload or a retry). */
  upsert: (document: KnowledgeDocument) => void;
  /** Drop a document (after a delete). */
  remove: (documentKey: string) => void;
  /** Re-read one document now (its ingest run ended). */
  refresh: (documentKey: string) => void;
}

function sortByKey(list: KnowledgeDocument[]): KnowledgeDocument[] {
  return list.sort((a, b) => (a.document_key < b.document_key ? -1 : a.document_key > b.document_key ? 1 : 0));
}

/**
 * A knowledge base's documents, a page at a time (by document_key, as the API orders them). While a
 * loaded document is pending or ingesting it is re-read every DOCUMENT_POLL_MS; `onSettled` is called
 * when one of them reaches ready or failed (the knowledge base's counts and cost changed).
 */
export function useKnowledgeDocuments(kbId: string, onSettled: (document: KnowledgeDocument) => void): KnowledgeDocumentsState {
  const [documents, setDocuments] = useState<KnowledgeDocument[]>([]);
  const [nextAfter, setNextAfter] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const generation = useRef(0);
  const docsRef = useRef<KnowledgeDocument[]>([]);
  docsRef.current = documents;
  const onSettledRef = useRef(onSettled);
  onSettledRef.current = onSettled;

  const reload = useCallback(() => {
    if (!kbId) return;
    const ticket = ++generation.current;
    setLoading(true);
    listDocuments(kbId, { limit: DOCUMENT_PAGE_SIZE }).then(
      (page) => {
        if (ticket !== generation.current) return;
        setDocuments(page.items);
        setNextAfter(page.next_after);
        setError(null);
        setLoading(false);
      },
      (reason: unknown) => {
        if (ticket !== generation.current) return;
        setError(reason instanceof Error ? reason.message : "Failed to load documents");
        setLoading(false);
      },
    );
  }, [kbId]);

  useEffect(() => {
    setDocuments([]);
    setNextAfter(null);
    reload();
    return () => {
      generation.current += 1;
    };
  }, [reload]);

  const loadMore = useCallback(() => {
    if (!nextAfter || loadingMore) return;
    const ticket = generation.current;
    setLoadingMore(true);
    listDocuments(kbId, { limit: DOCUMENT_PAGE_SIZE, after: nextAfter }).then(
      (page) => {
        if (ticket !== generation.current) return;
        setDocuments((current) => {
          const seen = new Set(current.map((doc) => doc.document_key));
          return [...current, ...page.items.filter((doc) => !seen.has(doc.document_key))];
        });
        setNextAfter(page.next_after);
        setLoadingMore(false);
      },
      (reason: unknown) => {
        if (ticket !== generation.current) return;
        setError(reason instanceof Error ? reason.message : "Failed to load more documents");
        setLoadingMore(false);
      },
    );
  }, [kbId, loadingMore, nextAfter]);

  const upsert = useCallback((document: KnowledgeDocument) => {
    setDocuments((current) => {
      const exists = current.some((doc) => doc.document_key === document.document_key);
      return exists ? current.map((doc) => (doc.document_key === document.document_key ? document : doc)) : sortByKey([...current, document]);
    });
  }, []);

  const remove = useCallback((documentKey: string) => {
    setDocuments((current) => current.filter((doc) => doc.document_key !== documentKey));
  }, []);

  /** Re-reads one document; false when the read failed (other than a 404, which drops the row). */
  const refreshOne = useCallback(
    async (documentKey: string, timeoutMs = 0): Promise<boolean> => {
      const before = docsRef.current.find((doc) => doc.document_key === documentKey);
      try {
        const fresh = timeoutMs > 0
          ? await withTimeout((signal) => getDocument(kbId, documentKey, { signal }), timeoutMs)
          : await getDocument(kbId, documentKey);
        if (!docsRef.current.some((doc) => doc.document_key === documentKey)) return true; // deleted meanwhile
        upsert(fresh);
        if (before && isIngestInFlight(before) && !isIngestInFlight(fresh)) onSettledRef.current(fresh);
        return true;
      } catch (reason: unknown) {
        if (reason instanceof ApiError && reason.status === 404) {
          remove(documentKey);
          return true;
        }
        return false;
      }
    },
    [kbId, remove, upsert],
  );

  const refresh = useCallback((documentKey: string) => {
    void refreshOne(documentKey);
  }, [refreshOne]);

  const inFlightKeys = documents.filter(isIngestInFlight).map((doc) => doc.document_key).slice(0, DOCUMENT_POLL_LIMIT).join("\n");
  // Every `pollDelay` ms; a tick is skipped while the previous one still waits for its answers (each
  // read gives up after DOCUMENT_POLL_TIMEOUT_MS, which counts as a failure). A
  // failing API is polled less often (3 s doubling to 30 s), back to 3 s once a poll succeeds; a
  // change of delay re-arms the interval.
  const [pollDelay, setPollDelay] = useState(DOCUMENT_POLL_MS);
  const polling = useRef(false);
  useEffect(() => {
    if (!inFlightKeys) return;
    let stopped = false;
    const timer = setInterval(() => {
      if (polling.current) return;
      polling.current = true;
      void Promise.all(inFlightKeys.split("\n").map((key) => refreshOne(key, DOCUMENT_POLL_TIMEOUT_MS)))
        .then((results) => {
          if (!stopped) setPollDelay((delay) => (results.every(Boolean) ? DOCUMENT_POLL_MS : Math.min(delay * 2, DOCUMENT_POLL_MAX_MS)));
        })
        .finally(() => {
          polling.current = false;
        });
    }, pollDelay);
    return () => {
      stopped = true;
      clearInterval(timer);
    };
  }, [inFlightKeys, refreshOne, pollDelay]);

  return { documents, loading, loadingMore, error, hasMore: nextAfter !== null, reload, loadMore, upsert, remove, refresh };
}
