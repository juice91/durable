"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { withSearchParams } from "@/components/ui/useUrlSheet";
import {
  failedOnlyParam,
  iterationParam,
  parseFailedOnlyParam,
  parseIterationParam,
  WORKFLOW_FAILED_ONLY_PARAM,
  WORKFLOW_ITERATION_PARAM,
} from "@/lib/routes/workflows";
import type { IterationSelection } from "@/lib/workflowRunState";

export interface RunIterations {
  /** The pagers' choices by loop instance key (`?iter=`). */
  selection: IterationSelection;
  setSelection: (next: IterationSelection) => void;
  /** Loop instances stepping through failed iterations only (`?failedOnly=`). */
  failedOnly: ReadonlySet<string>;
  setFailedOnly: (next: Set<string>) => void;
}

/**
 * The run view's iteration pagers as URL state (LP.12, the `?collapsed=` pattern of LP.6): the URL
 * is the source (a shared link to "iteration 42", Back, a reload); a change shows at once and is
 * written with `router.replace`, keeping the other parameters.
 *
 * Uses `useSearchParams`, so the page must sit inside a `<Suspense>` boundary.
 */
export function useRunIterations(): RunIterations {
  const router = useRouter();
  const pathname = usePathname() ?? "";
  const searchParams = useSearchParams();
  const rawIter = searchParams?.get(WORKFLOW_ITERATION_PARAM) ?? null;
  const rawFailed = searchParams?.get(WORKFLOW_FAILED_ONLY_PARAM) ?? null;
  const iterFromUrl = useMemo(() => parseIterationParam(rawIter), [rawIter]);
  const failedFromUrl = useMemo(() => parseFailedOnlyParam(rawFailed), [rawFailed]);
  const [selection, setSelectionState] = useState<IterationSelection>(iterFromUrl);
  const [failedOnly, setFailedState] = useState<ReadonlySet<string>>(failedFromUrl);

  // Follow the URL when it changes from elsewhere (Back, a pasted link).
  useEffect(() => setSelectionState(iterFromUrl), [iterFromUrl]);
  useEffect(() => setFailedState(failedFromUrl), [failedFromUrl]);

  // Two writes in one tick ("Failed only" also moves the pager) must both land: the second builds
  // on the first until the router reports the new URL.
  const pending = useRef<string | null>(null);
  useEffect(() => {
    pending.current = null;
  }, [searchParams]);
  const write = useCallback(
    (key: string, value: string | null) => {
      const live = typeof window !== "undefined" && window.location.pathname === pathname ? new URLSearchParams(window.location.search) : searchParams;
      const base = pending.current !== null ? new URLSearchParams(pending.current) : live;
      const href = withSearchParams(pathname, base, { [key]: value });
      pending.current = href.includes("?") ? href.slice(href.indexOf("?") + 1) : "";
      router.replace(href, { scroll: false });
    },
    [pathname, router, searchParams],
  );
  const setSelection = useCallback(
    (next: IterationSelection) => {
      setSelectionState(next);
      write(WORKFLOW_ITERATION_PARAM, iterationParam(next));
    },
    [write],
  );
  const setFailedOnly = useCallback(
    (next: Set<string>) => {
      setFailedState(next);
      write(WORKFLOW_FAILED_ONLY_PARAM, failedOnlyParam(next));
    },
    [write],
  );
  return { selection, setSelection, failedOnly, setFailedOnly };
}
