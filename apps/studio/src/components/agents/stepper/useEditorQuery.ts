"use client";

import { useCallback, useEffect, useRef, useState } from "react";
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
 * shared URL sets it); a change made here shows at once, keeping the other parameters. Steps are
 * written with `router.replace` (no Back entry per step). The compare Sheet opens with a push, so
 * Back closes it (as the tool and trace Sheets do); closing a Sheet this hook pushed goes Back, and
 * one that came with the URL (a link, a reload) closes by replacing.
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
    (changes: Record<string, string | null>, history: "push" | "replace" = "replace") => {
      const current = typeof window !== "undefined" && window.location.pathname === pathname ? new URLSearchParams(window.location.search) : searchParams;
      const href = withSearchParams(pathname, current, changes);
      if (history === "push") router.push(href, { scroll: false });
      else router.replace(href, { scroll: false });
    },
    [pathname, router, searchParams],
  );

  // The compare value this hook pushed, while its entry is on top (cleared when the URL moves away).
  const pushedCompare = useRef<string | null>(null);
  useEffect(() => {
    if (urlCompare === null || urlCompare !== pushedCompare.current) pushedCompare.current = null;
  }, [urlCompare]);

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
      // One Back entry per opening: switching the pair while our entry is on top replaces it.
      const ours = pushedCompare.current !== null;
      write({ compare: value }, ours ? "replace" : "push");
      pushedCompare.current = value;
    },
    [write],
  );

  const closeCompare = useCallback(() => {
    setCompareValue(null);
    if (pushedCompare.current !== null) {
      pushedCompare.current = null;
      router.back();
      return;
    }
    write({ compare: null });
  }, [router, write]);

  return { step, setStep, compare: parseCompareParam(compareValue), openCompare, closeCompare };
}
