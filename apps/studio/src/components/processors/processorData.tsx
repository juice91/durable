"use client";

import React, { createContext, useCallback, useContext, useEffect, useMemo, useState } from "react";
import { api, type Agent } from "@/lib/api";
import {
  loadAgentProcessorSpecs,
  processorUsage,
  processors as processorsApi,
  type ProcessorCatalog,
  type ProcessorType,
  type ProcessorUsage,
} from "@/lib/processors";

/** The window of the list's "Outcomes" column and the detail page's default outcome range. */
export const OUTCOME_WINDOW_MS = 24 * 60 * 60 * 1000;
/**
 * One page per type, at the API's maximum page size. A full page shows as "200+": the API has no
 * count endpoint, and paging through every outcome to count it is not worth N more requests.
 */
export const OUTCOME_COUNT_LIMIT = 200;
/** Shared processor data older than this is reloaded when a processors page mounts. */
export const PROCESSORS_DATA_TTL_MS = 60 * 1000;

export interface OutcomeCount {
  count: number;
  /** More outcomes exist than were counted (the page was full). */
  more: boolean;
}

function messageOf(reason: unknown, fallback: string): string {
  return reason instanceof Error ? reason.message : fallback;
}

/** "12", "200+", or "—" when the count is unknown. */
export function formatOutcomeCount(count: OutcomeCount | undefined): string {
  if (!count) return "—";
  return count.more ? `${count.count}+` : String(count.count);
}

/**
 * Outcomes per type since `now - 24 h`: one GET /processor-results per type (filtered by type and
 * `from`, one page of `OUTCOME_COUNT_LIMIT`). A type whose call fails is left out (shown as "—").
 * `signal` aborts every request (the list unmounted); the promise then rejects with the abort.
 */
export async function loadOutcomeCounts(
  types: ProcessorType[],
  now: number = Date.now(),
  signal?: AbortSignal,
): Promise<Partial<Record<ProcessorType, OutcomeCount>>> {
  const from = new Date(now - OUTCOME_WINDOW_MS).toISOString();
  const entries = await Promise.all(types.map(async (type) => {
    try {
      const page = await processorsApi.results({ type, from, limit: OUTCOME_COUNT_LIMIT }, signal);
      return [type, { count: page.items.length, more: page.next_before !== null }] as const;
    } catch {
      return null;
    }
  }));
  signal?.throwIfAborted();
  return Object.fromEntries(entries.filter((entry) => entry !== null)) as Partial<Record<ProcessorType, OutcomeCount>>;
}

export interface ProcessorsData {
  catalog: ProcessorCatalog | null;
  catalogError: string | null;
  agents: Agent[];
  usage: Record<ProcessorType, ProcessorUsage[]> | null;
  usageLoading: boolean;
  usageError: string | null;
}

interface SharedProcessorsData extends ProcessorsData {
  /** When the last load started (ms). */
  loadedAt: number;
  reload: () => void;
}

const EMPTY: ProcessorsData = { catalog: null, catalogError: null, agents: [], usage: null, usageLoading: true, usageError: null };

/**
 * Loads the catalog, the agents and which agents use each type. "Used by" costs one request per
 * published agent (its latest version's spec); a server-side usage endpoint would replace those N
 * requests with one. Until then the result is shared through `ProcessorsDataProvider`.
 */
function useProcessorsLoader(enabled: boolean): SharedProcessorsData {
  const [data, setData] = useState<ProcessorsData>(EMPTY);
  const [generation, setGeneration] = useState(0);
  const [loadedAt, setLoadedAt] = useState(0);

  useEffect(() => {
    if (!enabled) return;
    let cancelled = false;
    setLoadedAt(Date.now());
    setData((current) => ({ ...current, usageLoading: current.usage === null, catalogError: null, usageError: null }));
    processorsApi.catalog()
      .then((catalog) => { if (!cancelled) setData((current) => ({ ...current, catalog })); })
      .catch((reason: unknown) => {
        if (!cancelled) setData((current) => ({ ...current, catalogError: messageOf(reason, "Failed to load the processor catalog") }));
      });
    api.agents.list()
      .then(async (agents) => {
        if (cancelled) return;
        setData((current) => ({ ...current, agents }));
        const specs = await loadAgentProcessorSpecs(agents);
        if (!cancelled) setData((current) => ({ ...current, usage: processorUsage(specs) }));
      })
      .catch((reason: unknown) => {
        if (!cancelled) setData((current) => ({ ...current, usageError: messageOf(reason, "Failed to load agents") }));
      })
      .finally(() => { if (!cancelled) setData((current) => ({ ...current, usageLoading: false })); });
    return () => { cancelled = true; };
  }, [enabled, generation]);

  const reload = useCallback(() => setGeneration((n) => n + 1), []);
  return useMemo(() => ({ ...data, loadedAt, reload }), [data, loadedAt, reload]);
}

const ProcessorsDataContext = createContext<SharedProcessorsData | null>(null);

/**
 * Holds the processor data for every page under `/processors` (mounted by `app/processors/layout`),
 * so going from the list to a type and back does not repeat the agents and versions scan.
 */
export function ProcessorsDataProvider({ children }: { children: React.ReactNode }) {
  const value = useProcessorsLoader(true);
  return <ProcessorsDataContext.Provider value={value}>{children}</ProcessorsDataContext.Provider>;
}

/**
 * The shared processor data when under `ProcessorsDataProvider` (reloaded on mount once older than
 * `PROCESSORS_DATA_TTL_MS`); otherwise this component loads its own copy.
 */
export function useProcessorsData(): ProcessorsData {
  const shared = useContext(ProcessorsDataContext);
  const local = useProcessorsLoader(shared === null);
  const sharedLoadedAt = shared?.loadedAt;
  const sharedReload = shared?.reload;

  useEffect(() => {
    if (sharedReload && sharedLoadedAt !== undefined && sharedLoadedAt > 0 && Date.now() - sharedLoadedAt > PROCESSORS_DATA_TTL_MS) sharedReload();
    // On mount only: a page that stays open does not poll.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const source = shared ?? local;
  return {
    catalog: source.catalog,
    catalogError: source.catalogError,
    agents: source.agents,
    usage: source.usage,
    usageLoading: source.usageLoading,
    usageError: source.usageError,
  };
}
