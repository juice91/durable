"use client";

import React, { useEffect, useRef, useState } from "react";
import { ProcessorCatalog } from "@/components/processors/ProcessorCatalog";
import { ProcessorResultsTable } from "@/components/processors/ProcessorResultsTable";
import { ProcessorTestConsole } from "@/components/processors/ProcessorTestConsole";
import { api, type Agent } from "@/lib/api";
import {
  loadAgentProcessorSpecs,
  processorUsage,
  processors as processorsApi,
  type ProcessorCatalog as Catalog,
  type ProcessorType,
  type ProcessorUsage,
} from "@/lib/processors";

function messageOf(reason: unknown, fallback: string): string {
  return reason instanceof Error ? reason.message : fallback;
}

export default function ProcessorsPage() {
  const [catalog, setCatalog] = useState<Catalog | null>(null);
  const [catalogError, setCatalogError] = useState<string | null>(null);
  const [agents, setAgents] = useState<Agent[]>([]);
  const [usage, setUsage] = useState<Record<ProcessorType, ProcessorUsage[]> | null>(null);
  const [usageLoading, setUsageLoading] = useState(true);
  const [usageError, setUsageError] = useState<string | null>(null);
  // Remounts the console with a type picked from a catalog card.
  const [consoleSeed, setConsoleSeed] = useState<{ type?: ProcessorType; nonce: number }>({ nonce: 0 });
  const consoleRef = useRef<HTMLElement>(null);

  useEffect(() => {
    let cancelled = false;
    processorsApi.catalog()
      .then((data) => { if (!cancelled) setCatalog(data); })
      .catch((reason: unknown) => { if (!cancelled) setCatalogError(messageOf(reason, "Failed to load the processor catalog")); });
    api.agents.list()
      .then(async (list) => {
        if (cancelled) return;
        setAgents(list);
        const specs = await loadAgentProcessorSpecs(list);
        if (!cancelled) setUsage(processorUsage(specs));
      })
      .catch((reason: unknown) => { if (!cancelled) setUsageError(messageOf(reason, "Failed to load agents")); })
      .finally(() => { if (!cancelled) setUsageLoading(false); });
    return () => { cancelled = true; };
  }, []);

  const tryType = (type: ProcessorType) => {
    setConsoleSeed((current) => ({ type, nonce: current.nonce + 1 }));
    setTimeout(() => consoleRef.current?.scrollIntoView?.({ behavior: "smooth", block: "start" }), 0);
  };

  return (
    <div className="mx-auto max-w-7xl space-y-10 px-4 py-8">
      <div>
        <h1 className="text-2xl font-bold text-foreground">Processors</h1>
        <p className="mt-1 text-muted-foreground">
          Guardrails that run on an agent&apos;s input and output: the built-in types, which agents use them,
          what they did on recent runs, and a console to try them on a sample.
        </p>
      </div>

      <section aria-labelledby="processors-catalog" className="space-y-3">
        <h2 id="processors-catalog" className="text-lg font-semibold text-foreground">Catalog</h2>
        {catalogError ? <p role="alert" data-testid="catalog-error" className="rounded-md border border-destructive/30 bg-destructive/10 p-3 text-sm text-destructive-text">{catalogError}</p> : null}
        {!catalog && !catalogError ? <p className="text-sm text-muted-foreground">Loading processor types…</p> : null}
        {catalog && catalog.items.length === 0 ? <p data-testid="catalog-empty" className="text-sm text-muted-foreground">No processor types are available.</p> : null}
        {catalog && catalog.items.length > 0 ? (
          <ProcessorCatalog items={catalog.items} usage={usage} usageLoading={usageLoading} usageError={usageError} onTry={tryType} />
        ) : null}
      </section>

      <section aria-labelledby="processors-results" className="space-y-3">
        <h2 id="processors-results" className="text-lg font-semibold text-foreground">Recent outcomes</h2>
        <ProcessorResultsTable
          agents={agents.map(({ id, name }) => ({ id, name }))}
          types={(catalog?.items ?? []).map(({ id, title }) => ({ id, title }))}
        />
      </section>

      <section ref={consoleRef} aria-labelledby="processors-console" className="space-y-3">
        <div>
          <h2 id="processors-console" className="text-lg font-semibold text-foreground">Test console</h2>
          <p className="text-sm text-muted-foreground">Run deterministic processors on a sample. Nothing is stored and no agent runs.</p>
        </div>
        {catalog ? (
          <ProcessorTestConsole key={consoleSeed.nonce} catalog={catalog} initialType={consoleSeed.type} />
        ) : (
          <p className="text-sm text-muted-foreground">{catalogError ? "The test console needs the processor catalog." : "Loading…"}</p>
        )}
      </section>
    </div>
  );
}
