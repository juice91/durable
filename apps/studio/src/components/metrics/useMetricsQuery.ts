"use client";

import { useEffect, useState } from "react";
import { metricsErrorMessage } from "@/lib/metrics";

export interface MetricsQueryState<T> {
  data: T | null;
  error: string | null;
  loading: boolean;
}

/** Run `load` whenever `key` changes (aborting the previous call); keeps the last data while loading.
 * `key === null` skips the call (e.g. an invalid range). */
export function useMetricsQuery<T>(key: string | null, load: (signal: AbortSignal) => Promise<T>): MetricsQueryState<T> {
  const [state, setState] = useState<MetricsQueryState<T>>({ data: null, error: null, loading: key !== null });

  useEffect(() => {
    if (key === null) {
      setState({ data: null, error: null, loading: false });
      return;
    }
    const controller = new AbortController();
    setState((s) => ({ ...s, error: null, loading: true }));
    load(controller.signal).then(
      (data) => {
        if (!controller.signal.aborted) setState({ data, error: null, loading: false });
      },
      (error: unknown) => {
        if (controller.signal.aborted) return;
        setState({ data: null, error: metricsErrorMessage(error), loading: false });
      },
    );
    return () => controller.abort();
    // `load` is recreated each render; `key` captures everything it depends on.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key]);

  return state;
}
