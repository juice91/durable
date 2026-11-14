"use client";

import { useCallback, useRef } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { withSheetParams } from "./TraceSheetHost";
import { TRACE_TAB_PARAM } from "@/lib/routes/observability";

export interface PendingQueryOptions<F> {
  /** The filters the current URL shows. */
  filters: F;
  /** The page's filters as URL parameters (without the trace Sheet's). */
  toParams: (filters: F) => URLSearchParams;
  /** The page URL for those parameters (`routes.logs`, `routes.traces`). */
  href: (params: URLSearchParams) => string;
  /** The trace Sheet's tab parameter on this page (default `tab`). */
  tabParam?: string;
}

export interface PendingQuery<F> {
  /** Merge `patch` into the filters as last written and write them (router.replace, Sheet params kept). */
  setFilters: (patch: Partial<F>) => void;
  /** Write these filters, replacing all of them. */
  writeFilters: (filters: F) => void;
  /** The filters as last written (ahead of the URL while a write is in flight). */
  pendingFilters: () => F;
  /**
   * The query last written and not yet in the URL, or null when the URL is current. Pass it to
   * `<TraceSheetHost baseParams>`.
   */
  pendingParams: () => URLSearchParams | null;
}

/**
 * A filter page's writes to its URL, kept ahead of the URL (SL.15, Logs and Traces).
 *
 * A text filter commits on blur with `router.replace`. When that blur comes from clicking a row's
 * Trace link, the link opens the trace Sheet in the same tick, and Next 15 drops a replace still
 * pending when a new navigation arrives: the filter would be lost and its box reset. So every write
 * is remembered until the URL changes; later writes merge into it, and the trace Sheet builds on
 * it (`pendingParams` as the host's `baseParams`, which then opens by replacing, see `useUrlSheet`).
 */
export function usePendingQuery<F>({ filters, toParams, href, tabParam = TRACE_TAB_PARAM }: PendingQueryOptions<F>): PendingQuery<F> {
  const router = useRouter();
  const searchParams = useSearchParams();
  const urlKey = searchParams?.toString() ?? "";
  const pending = useRef<{ urlKey: string; filters: F; written: URLSearchParams | null }>({ urlKey, filters, written: null });
  // The URL moved (a write landed, Back, a link): it is the truth again.
  if (pending.current.urlKey !== urlKey) pending.current = { urlKey, filters, written: null };
  // The latest URL: filter writes keep an open trace Sheet's `trace` and tab.
  const latestParams = useRef(searchParams);
  latestParams.current = searchParams;
  const options = useRef({ toParams, href, tabParam });
  options.current = { toParams, href, tabParam };

  const writeFilters = useCallback(
    (next: F) => {
      const { toParams: paramsOf, href: hrefOf, tabParam: tab } = options.current;
      const params = withSheetParams(paramsOf(next), pending.current.written ?? latestParams.current, tab);
      pending.current = { ...pending.current, filters: next, written: params };
      router.replace(hrefOf(params), { scroll: false });
    },
    [router],
  );
  const setFilters = useCallback((patch: Partial<F>) => writeFilters({ ...pending.current.filters, ...patch }), [writeFilters]);
  const pendingFilters = useCallback(() => pending.current.filters, []);
  const pendingParams = useCallback(() => pending.current.written, []);
  return { setFilters, writeFilters, pendingFilters, pendingParams };
}
