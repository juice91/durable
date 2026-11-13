"use client";

import React, { useEffect, useState } from "react";
import { PageHeader } from "@/components/frame/PageHeader";
import { PROCESSOR_SCOPE_NOTE } from "@/components/processors/ProcessorCatalog";
import { ProcessorList } from "@/components/processors/ProcessorList";
import { loadOutcomeCounts, useProcessorsData, type OutcomeCount } from "@/components/processors/processorData";
import type { ProcessorType } from "@/lib/processors";
import { navCrumb } from "@/lib/nav";

/** `/processors`: the catalog of built-in processor types; a row opens `/processors/[type]`. */
export default function ProcessorsPage() {
  const { catalog, catalogError, usage, usageLoading, usageError } = useProcessorsData();
  const [outcomeCounts, setOutcomeCounts] = useState<Partial<Record<ProcessorType, OutcomeCount>> | null>(null);

  useEffect(() => {
    if (!catalog) return;
    const controller = new AbortController();
    loadOutcomeCounts(catalog.items.map((item) => item.id), Date.now(), controller.signal)
      .then((counts) => setOutcomeCounts(counts))
      .catch(() => { /* aborted on unmount */ });
    return () => controller.abort();
  }, [catalog]);

  return (
    <>
      <PageHeader crumbs={[navCrumb("processors", { current: true })]} />
      <div className="mx-auto w-full min-w-0 max-w-7xl space-y-4 px-4 py-6">
        <div className="space-y-1">
          <p className="text-body text-muted-foreground">
            Guardrails that run on an agent&apos;s input and output. Open a type for its configuration, a test
            console, recent outcomes and the agents that use it.
          </p>
          <p className="text-caption text-muted-foreground">{PROCESSOR_SCOPE_NOTE}</p>
        </div>
        {usageError ? <p role="alert" data-testid="usage-error" className="text-label text-destructive-text">{usageError}</p> : null}
        {catalogError ? (
          <p role="alert" data-testid="catalog-error" className="rounded-md border border-destructive/30 bg-destructive/10 p-3 text-label text-destructive-text">{catalogError}</p>
        ) : catalog && catalog.items.length === 0 ? (
          <p data-testid="catalog-empty" className="text-label text-muted-foreground">No processor types are available.</p>
        ) : (
          <ProcessorList
            items={catalog?.items ?? []}
            loading={!catalog}
            usage={usage}
            usageLoading={usageLoading}
            usageError={usageError}
            outcomeCounts={outcomeCounts}
          />
        )}
      </div>
    </>
  );
}
