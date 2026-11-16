"use client";

import { useCallback, useEffect, useMemo, useState } from "react";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { withSearchParams } from "@/components/ui/useUrlSheet";
import { collapsedParam, parseCollapsedParam, WORKFLOW_COLLAPSED_PARAM } from "@/lib/routes/workflows";

export interface CollapsedLoops {
  /** The loop step ids drawn collapsed (from `?collapsed=`). */
  collapsed: ReadonlySet<string>;
  setCollapsed: (next: Set<string>) => void;
}

/**
 * The graph's collapsed loops as URL state (`?collapsed=a,b`, LP.6): the URL is the source (a
 * shared link, Back, a reload, a version switch); a change shows at once and is written with
 * `router.replace`, keeping the other parameters (no Back entry per toggle).
 *
 * Uses `useSearchParams`, so the page must sit inside a `<Suspense>` boundary.
 */
export function useCollapsedLoops(): CollapsedLoops {
  const router = useRouter();
  const pathname = usePathname() ?? "";
  const searchParams = useSearchParams();
  const raw = searchParams?.get(WORKFLOW_COLLAPSED_PARAM) ?? null;
  const fromUrl = useMemo(() => parseCollapsedParam(raw), [raw]);
  const [collapsed, setState] = useState<ReadonlySet<string>>(fromUrl);

  // Follow the URL when it changes from elsewhere (Back, a pasted link).
  useEffect(() => setState(fromUrl), [fromUrl]);

  const setCollapsed = useCallback(
    (next: Set<string>) => {
      setState(next);
      const current = typeof window !== "undefined" && window.location.pathname === pathname ? new URLSearchParams(window.location.search) : searchParams;
      router.replace(withSearchParams(pathname, current, { [WORKFLOW_COLLAPSED_PARAM]: collapsedParam(next) }), { scroll: false });
    },
    [pathname, router, searchParams],
  );

  return { collapsed, setCollapsed };
}
