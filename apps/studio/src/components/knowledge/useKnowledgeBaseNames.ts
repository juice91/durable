"use client";

import { useEffect, useState } from "react";
import { listKnowledgeBases } from "@/lib/knowledge";

/** A loaded names map is reused for this long (as the palette's entity cache). */
export const KB_NAMES_FRESH_MS = 60_000;

let cache: { at: number; names: Record<string, string> } | null = null;
let inflight: Promise<Record<string, string>> | null = null;

/** Tests only. */
export function resetKnowledgeBaseNames(): void {
  cache = null;
  inflight = null;
}

function loadNames(): Promise<Record<string, string>> {
  if (cache && Date.now() - cache.at < KB_NAMES_FRESH_MS) return Promise.resolve(cache.names);
  inflight ??= listKnowledgeBases()
    .then((items) => {
      cache = { at: Date.now(), names: Object.fromEntries(items.map((kb) => [kb.id, kb.name])) };
      return cache.names;
    })
    .finally(() => {
      inflight = null;
    });
  return inflight;
}

/**
 * Knowledge base id → name, for labelling kb_ingest runs (RunSubject). Fetched only while `enabled`
 * (a page shows such a run), shared across callers and cached for a minute; a failure leaves `{}`,
 * and the run's link still works without the name.
 */
export function useKnowledgeBaseNames(enabled: boolean): Record<string, string> {
  const [names, setNames] = useState<Record<string, string>>(() => cache?.names ?? {});
  useEffect(() => {
    if (!enabled) return;
    let cancelled = false;
    loadNames().then(
      (loaded) => {
        if (!cancelled) setNames(loaded);
      },
      () => undefined,
    );
    return () => {
      cancelled = true;
    };
  }, [enabled]);
  return names;
}
