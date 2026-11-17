"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError } from "@/lib/api";
import type { Page } from "@/lib/evalOverview";

export interface PagedList<T> {
  rows: T[];
  /** First page loading. */
  loading: boolean;
  loadingMore: boolean;
  /** Error of the first page (the list is replaced by the error state). */
  error: string | null;
  /** Error of a later page (the loaded rows stay). */
  moreError: string | null;
  hasMore: boolean;
  reload: () => void;
  loadMore: () => void;
}

/**
 * A cursor-paginated list: the first page on mount (and on `reload`), further pages on `loadMore`.
 * `fetchPage(cursor, signal)` returns `{items, next_cursor}`.
 */
export function usePagedList<T>(fetchPage: (cursor: string | null, signal: AbortSignal) => Promise<Page<T>>, fallback: string, deps: readonly unknown[] = []): PagedList<T> {
  const [rows, setRows] = useState<T[]>([]);
  const [cursor, setCursor] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [moreError, setMoreError] = useState<string | null>(null);
  const [nonce, setNonce] = useState(0);
  const fetchRef = useRef(fetchPage);
  fetchRef.current = fetchPage;
  const controller = useRef<AbortController | null>(null);

  const message = (reason: unknown) => (reason instanceof ApiError || reason instanceof Error ? reason.message : fallback);

  useEffect(() => {
    const abort = new AbortController();
    controller.current = abort;
    setLoading(true);
    setError(null);
    setMoreError(null);
    fetchRef.current(null, abort.signal).then(
      (page) => {
        if (abort.signal.aborted) return;
        setRows(page.items);
        setCursor(page.next_cursor);
        setLoading(false);
      },
      (reason: unknown) => {
        if (abort.signal.aborted) return;
        setError(message(reason));
        setLoading(false);
      },
    );
    return () => abort.abort();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [nonce, ...deps]);

  const loadMore = useCallback(() => {
    if (cursor === null || loadingMore) return;
    const abort = controller.current ?? new AbortController();
    setLoadingMore(true);
    setMoreError(null);
    fetchRef.current(cursor, abort.signal).then(
      (page) => {
        if (abort.signal.aborted) return;
        setRows((current) => [...current, ...page.items]);
        setCursor(page.next_cursor);
        setLoadingMore(false);
      },
      (reason: unknown) => {
        if (abort.signal.aborted) return;
        setMoreError(message(reason));
        setLoadingMore(false);
      },
    );
  }, [cursor, loadingMore]);

  const reload = useCallback(() => setNonce((n) => n + 1), []);

  return { rows, loading, loadingMore, error, moreError, hasMore: cursor !== null, reload, loadMore };
}
