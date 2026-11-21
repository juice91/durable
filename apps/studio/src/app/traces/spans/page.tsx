"use client";

import React, { Suspense, useEffect, useMemo, useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { api } from "@/lib/api";
import { EMPTY_SPAN_FILTERS, parseSpanFilters, spanFiltersToParams, type SpanFilters } from "@/lib/spans";
import { KIND_LABELS, SPAN_KINDS } from "@/lib/traces";
import { routes } from "@/lib/routes";
import { TRACE_SPAN_PARAM } from "@/lib/routes/observability";
import { navCrumb } from "@/lib/nav";
import { PageHeader } from "@/components/frame/PageHeader";
import { SpansList } from "@/components/traces/SpansList";
import { TraceFilterBar, filterInputClass, filterLabelClass } from "@/components/traces/TraceFilterBar";
import { TraceSheetHost } from "@/components/traces/TraceSheetHost";
import { TracesTabs } from "@/components/traces/TracesTabs";
import { usePendingQuery } from "@/components/traces/usePendingQuery";
import { Select } from "@/components/ui/Select";

function SpansPageBody() {
  const searchParams = useSearchParams();
  const router = useRouter();
  // Only the filters: opening or closing the trace Sheet must not reload the list.
  const filterKey = spanFiltersToParams(parseSpanFilters(new URLSearchParams(searchParams?.toString() ?? ""))).toString();
  const filters = useMemo(() => parseSpanFilters(new URLSearchParams(filterKey)), [filterKey]);
  const openSpanId = searchParams?.get(TRACE_SPAN_PARAM) || null;
  const [agentNames, setAgentNames] = useState<Record<string, string>>({});

  useEffect(() => {
    let cancelled = false;
    void api.agents.list().then((agents) => {
      if (!cancelled) setAgentNames(Object.fromEntries(agents.map((agent) => [agent.id, agent.name])));
    }).catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, []);

  const { setFilters, writeFilters, pendingParams } = usePendingQuery<SpanFilters>({
    filters,
    toParams: spanFiltersToParams,
    href: routes.spans,
  });

  return (
    <>
      <PageHeader crumbs={[navCrumb("traces", { current: true })]} />
      <TracesTabs active="spans" />
      <TraceSheetHost baseParams={pendingParams}>
        {(sheet) => (
          <div className="flex min-w-0 flex-col gap-4 p-4">
            <p className="text-label text-muted-foreground">Every span of the finished runs, newest first</p>
            <TraceFilterBar
              filters={filters}
              setFilters={(patch) => setFilters(patch)}
              onClear={() => writeFilters(EMPTY_SPAN_FILTERS)}
              agentNames={agentNames}
              searchPlaceholder="span name"
              extraActive={filters.span_kind !== null}
              extra={
                <div className="flex min-w-0 flex-col gap-1">
                  <label htmlFor="span-kind-filter" className={filterLabelClass}>Span kind</label>
                  <Select
                    id="span-kind-filter"
                    value={filters.span_kind ?? "all"}
                    onValueChange={(v) => setFilters({ span_kind: v === "all" ? null : (v as SpanFilters["span_kind"]) })}
                    className={filterInputClass}
                  >
                    <option value="all">All</option>
                    {SPAN_KINDS.map((kind) => (
                      <option key={kind} value={kind}>{KIND_LABELS[kind]}</option>
                    ))}
                  </Select>
                </div>
              }
            />
            <SpansList filters={filters} onOpenExternal={(traceId) => router.push(routes.traces({ source: "external", external: traceId }))} onOpen={(runId, spanId) => sheet.open(runId, null, spanId)} selectedRunId={sheet.runId} selectedSpanId={openSpanId} />
          </div>
        )}
      </TraceSheetHost>
    </>
  );
}

export default function SpansPage() {
  return (
    <Suspense fallback={<><PageHeader crumbs={[navCrumb("traces", { current: true })]} /><p className="py-12 text-center text-muted-foreground">Loading spans...</p></>}>
      <SpansPageBody />
    </Suspense>
  );
}
