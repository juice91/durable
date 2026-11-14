"use client";

import { useCallback, useEffect, useState } from "react";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { withSearchParams } from "@/components/ui/useUrlSheet";
import { compareParam, parseCompareParam, type CompareRouteRef } from "@/lib/routes/agents";
import { EDITOR_STEPS, normalizeStepId } from "./registry";
import type { EditorStepDefinition } from "./types";

export interface EditorCompare {
  base: CompareRouteRef;
  target: CompareRouteRef;
}

export interface EditorQuery {
  /** The current step id (the first step when `?step=` is missing or unknown). */
  step: string;
  setStep: (id: string) => void;
  /** The open compare (`?compare=a..b`), or null. */
  compare: EditorCompare | null;
  openCompare: (base: CompareRouteRef, target: CompareRouteRef) => void;
  closeCompare: () => void;
}

/**
 * The editor's URL state: `?step=` and `?compare=a..b`. The URL is the source (a link, Back or a
 * shared URL sets it); a change made here shows at once and is written with `router.replace` (no
 * Back entry per step), keeping the other parameters.
 *
 * Uses `useSearchParams`, so the page must sit inside a `<Suspense>` boundary.
 */
export function useEditorQuery(steps: readonly EditorStepDefinition[] = EDITOR_STEPS): EditorQuery {
  const router = useRouter();
  const pathname = usePathname() ?? "";
  const searchParams = useSearchParams();
  const urlStep = searchParams?.get("step") ?? null;
  const urlCompare = searchParams?.get("compare") ?? null;
  const [step, setStepState] = useState(() => normalizeStepId(urlStep, steps));
  const [compareValue, setCompareValue] = useState<string | null>(urlCompare);

  // Follow the URL when it changes from elsewhere (a Config "Edit" link, Back, a pasted URL).
  useEffect(() => setStepState(normalizeStepId(urlStep, steps)), [urlStep, steps]);
  useEffect(() => setCompareValue(urlCompare), [urlCompare]);

  const write = useCallback(
    (changes: Record<string, string | null>) => {
      const current = typeof window !== "undefined" && window.location.pathname === pathname ? new URLSearchParams(window.location.search) : searchParams;
      router.replace(withSearchParams(pathname, current, changes), { scroll: false });
    },
    [pathname, router, searchParams],
  );

  const setStep = useCallback(
    (id: string) => {
      const next = normalizeStepId(id, steps);
      setStepState(next);
      write({ step: next });
    },
    [steps, write],
  );

  const openCompare = useCallback(
    (base: CompareRouteRef, target: CompareRouteRef) => {
      const value = compareParam(base, target);
      setCompareValue(value);
      write({ compare: value });
    },
    [write],
  );

  const closeCompare = useCallback(() => {
    setCompareValue(null);
    write({ compare: null });
  }, [write]);

  return { step, setStep, compare: parseCompareParam(compareValue), openCompare, closeCompare };
}
