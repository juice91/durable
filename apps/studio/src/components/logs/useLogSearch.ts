"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import {
  PAGE_LIMIT,
  appendOlder,
  classifyLogsError,
  olderPageTo,
  searchLogs,
  type LogItem,
  type LogsError,
  type LogsQueryParams,
} from "./logsQuery";

export interface LogSearchState {
  items: LogItem[];
  /** The first page for the current key is in flight (items shown, if any, are the previous ones). */
  loading: boolean;
  error: LogsError | null;
  /** The last page was full, or the agent filter left older runs out (`agent_runs_limited`: the
   * next page's narrower window covers them), so older records may exist. */
  hasOlder: boolean;
  loadingOlder: boolean;
  olderError: string | null;
  /** The window the API searched for the current results. */
  window: { from: string; to: string } | null;
  /** With an agent filter: the agent's runs the API searched (`agent_runs`, `agent_runs_limited`). */
  agentRuns: { count: number; limited: boolean } | null;
}

const INITIAL: LogSearchState = {
  items: [], loading: false, error: null, hasOlder: false, loadingOlder: false, olderError: null, window: null, agentRuns: null,
};

export type LogSearch = LogSearchState & { loadOlder: () => void };

/** GET /logs for `key` (null = no request): a new key aborts and supersedes every request of the
 * previous one, including a "load older" in flight. Each request carries the generation it was
 * started in and is applied only while that generation is current, so an out-of-order response
 * (or an older page fetched with a cursor from previous filters) never replaces or extends newer
 * results. `params` is read when the key changes; `key` must capture everything it depends on. */
export function useLogSearch(key: string | null, params: () => LogsQueryParams): LogSearch {
  const [state, setState] = useState<LogSearchState>(INITIAL);
  const paramsRef = useRef(params);
  paramsRef.current = params;

  const generationRef = useRef(0);
  const itemsRef = useRef<LogItem[]>([]);
  itemsRef.current = state.items;
  // The current generation's first-page query and the window the API answered for: the cursor base.
  const pageRef = useRef<{ params: LogsQueryParams; from: string; limit: number } | null>(null);
  const olderRef = useRef<AbortController | null>(null);

  useEffect(() => {
    const generation = ++generationRef.current;
    pageRef.current = null;
    olderRef.current?.abort();
    olderRef.current = null;
    if (key === null) {
      setState(INITIAL);
      return;
    }
    const controller = new AbortController();
    const query = paramsRef.current();
    const limit = query.limit ?? PAGE_LIMIT;
    setState((s) => ({ ...s, loading: true, error: null, hasOlder: false, loadingOlder: false, olderError: null }));
    searchLogs(query, controller.signal).then(
      (data) => {
        if (generation !== generationRef.current) return;
        const items = Array.isArray(data.items) ? data.items : [];
        pageRef.current = { params: query, from: data.from, limit };
        setState({
          ...INITIAL,
          items,
          hasOlder: items.length >= limit || (data.agent_runs_limited === true && items.length > 0),
          window: { from: data.from, to: data.to },
          agentRuns: typeof data.agent_runs === "number" ? { count: data.agent_runs, limited: data.agent_runs_limited === true } : null,
        });
      },
      (error: unknown) => {
        if (generation !== generationRef.current || controller.signal.aborted) return;
        setState({ ...INITIAL, error: classifyLogsError(error) });
      },
    );
    return () => controller.abort();
  }, [key]);

  useEffect(() => () => olderRef.current?.abort(), []);

  const loadOlder = useCallback(() => {
    const page = pageRef.current;
    if (!page || olderRef.current) return;
    const to = olderPageTo(itemsRef.current);
    if (!to) return;
    const generation = generationRef.current;
    const controller = new AbortController();
    olderRef.current = controller;
    setState((s) => ({ ...s, loadingOlder: true, olderError: null }));
    searchLogs({ ...page.params, from: page.from, to }, controller.signal).then(
      (data) => {
        if (generation !== generationRef.current) return;
        olderRef.current = null;
        const older = Array.isArray(data.items) ? data.items : [];
        const { items, added } = appendOlder(itemsRef.current, older);
        // No new record means the remaining ones share one microsecond: stop instead of looping.
        const more = older.length >= page.limit || data.agent_runs_limited === true;
        setState((s) => ({ ...s, items, loadingOlder: false, hasOlder: added > 0 && more }));
      },
      (error: unknown) => {
        if (generation !== generationRef.current || controller.signal.aborted) return;
        olderRef.current = null;
        setState((s) => ({ ...s, loadingOlder: false, olderError: classifyLogsError(error).message }));
      },
    );
  }, []);

  return { ...state, loadOlder };
}
