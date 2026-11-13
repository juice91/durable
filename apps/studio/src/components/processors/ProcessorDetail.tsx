"use client";

import Link from "next/link";
import React from "react";
import { PageHeader } from "@/components/frame/PageHeader";
import { ProcessorOverview, ProcessorUsageList } from "@/components/processors/ProcessorCatalog";
import { ProcessorResultsTable } from "@/components/processors/ProcessorResultsTable";
import { ProcessorTestConsole } from "@/components/processors/ProcessorTestConsole";
import { OUTCOME_WINDOW_MS, useProcessorsData } from "@/components/processors/processorData";
import { EmptyState } from "@/components/shared/EmptyState";
import { buttonVariants } from "@/components/ui/Button";
import { MODERATION_TEST_MESSAGE } from "@/lib/processors";
import { navCrumb } from "@/lib/nav";
import { routes } from "@/lib/routes";

/**
 * `/processors/[type]`: one processor type. The overview (config schema, limits, scope note), the
 * test console (22rem form column, result beside it; stacked below 1024 px), recent outcomes of
 * this type with run links, and the agents and versions that use it. An unknown type is not found.
 */
export function ProcessorDetail({ type }: { type: string }) {
  const { catalog, catalogError, agents, usage, usageLoading, usageError } = useProcessorsData();
  const item = catalog?.items.find((candidate) => candidate.id === type) ?? null;
  const notFound = catalog !== null && item === null;

  return (
    <>
      <PageHeader crumbs={[navCrumb("processors"), { label: item?.title ?? type, testId: "processor-title" }]} />
      <div className="mx-auto w-full min-w-0 max-w-7xl space-y-8 px-4 py-6">
        {catalogError ? (
          <p role="alert" data-testid="catalog-error" className="rounded-md border border-destructive/30 bg-destructive/10 p-3 text-label text-destructive-text">{catalogError}</p>
        ) : null}
        {!catalog && !catalogError ? <p className="text-label text-muted-foreground">Loading processor type…</p> : null}
        {catalogError ? <p className="text-label text-muted-foreground">The test console needs the processor catalog.</p> : null}

        {notFound ? (
          <EmptyState
            testId="processor-not-found"
            title="Processor type not found"
            description={<>There is no processor type <span className="font-mono">{type}</span> in the catalog.</>}
            action={<Link href={routes.processors()} className={buttonVariants({ variant: "secondary" })}>All processors</Link>}
          />
        ) : null}

        {catalog && item ? (
          <>
            <ProcessorOverview item={item} limits={catalog.limits} />

            <section aria-labelledby="processors-console" className="min-w-0 space-y-3">
              <div>
                <h2 id="processors-console" className="text-title font-semibold text-foreground">Test console</h2>
                <p className="text-label text-muted-foreground">Run deterministic processors on a sample. Nothing is stored and no agent runs.</p>
              </div>
              {item.deterministic ? (
                <ProcessorTestConsole key={item.id} catalog={catalog} initialType={item.id} />
              ) : (
                <p data-testid="console-unavailable" className="rounded-md border border-dashed border-border p-4 text-label text-muted-foreground">{MODERATION_TEST_MESSAGE}</p>
              )}
            </section>

            <section aria-labelledby="processors-results" className="min-w-0 space-y-3">
              <h2 id="processors-results" className="text-title font-semibold text-foreground">Recent outcomes</h2>
              <ProcessorResultsTable
                key={item.id}
                fixedType={item.id}
                defaultWindowMs={OUTCOME_WINDOW_MS}
                agents={agents.map(({ id, name }) => ({ id, name }))}
                types={catalog.items.map(({ id, title }) => ({ id, title }))}
              />
            </section>

            <section aria-labelledby="processors-used-by" className="min-w-0 space-y-3">
              <h2 id="processors-used-by" className="text-title font-semibold text-foreground">Used by</h2>
              <div className="rounded-lg border border-border bg-surface px-4 py-2">
                <ProcessorUsageList usage={usage?.[item.id]} loading={usageLoading} error={usageError} />
              </div>
            </section>
          </>
        ) : null}
      </div>
    </>
  );
}
