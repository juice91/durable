"use client";

import React, { useMemo } from "react";
import { DataList, type DataListColumn } from "@/components/shared/DataList";
import { formatOutcomeCount, type OutcomeCount } from "@/components/processors/processorData";
import { PROCESSOR_PHASE_LABELS, PROCESSOR_PHASES, type ProcessorDeclaration, type ProcessorType, type ProcessorUsage } from "@/lib/processors";
import { routes } from "@/lib/routes";

/** "3 agents", "1 agent", "1 agent (1 unknown)"; null while unknown. */
export function usageSummary(usage: ProcessorUsage[] | undefined): string | null {
  if (!usage) return null;
  const unknown = usage.filter((item) => item.unknown).length;
  const known = usage.length - unknown;
  const base = `${known} ${known === 1 ? "agent" : "agents"}`;
  return unknown > 0 ? `${base} (${unknown} unknown)` : base;
}

export interface ProcessorListProps {
  items: ProcessorDeclaration[];
  usage: Record<ProcessorType, ProcessorUsage[]> | null;
  usageLoading: boolean;
  usageError: string | null;
  /** Outcomes in the last 24 h per type; null while loading. */
  outcomeCounts: Partial<Record<ProcessorType, OutcomeCount>> | null;
  loading?: boolean;
}

/**
 * The processor catalog as a DataList: Type, Description, Input, Step, Stream, Output, Used by, Outcomes (24 h). A row
 * opens `/processors/[type]`.
 */
export function ProcessorList({ items, usage, usageLoading, usageError, outcomeCounts, loading = false }: ProcessorListProps) {
  const columns = useMemo<DataListColumn<ProcessorDeclaration>[]>(() => [
    {
      id: "type",
      header: "Type",
      sortable: true,
      sortValue: (item) => item.title,
      cell: (item) => (
        <span className="flex min-w-0 flex-col">
          <span className="truncate">{item.title}</span>
          <span className="truncate font-mono text-caption font-normal text-muted-foreground">{item.id}</span>
        </span>
      ),
      className: "min-w-40",
    },
    {
      id: "description",
      header: "Description",
      cell: (item) => <span className="line-clamp-2 text-muted-foreground">{item.description}</span>,
      className: "hidden min-w-48 md:table-cell",
    },
    ...PROCESSOR_PHASES.map((phase): DataListColumn<ProcessorDeclaration> => ({
      id: `phase-${phase}`,
      header: PROCESSOR_PHASE_LABELS[phase],
      cell: (item) =>
        item.phases.includes(phase) ? (
          <span data-testid={`phase-${phase}`} role="img" aria-label={`Runs in the ${phase} phase`}>✓</span>
        ) : (
          <span data-testid={`phase-${phase}`} role="img" aria-label={`Does not run in the ${phase} phase`} className="text-muted-foreground">—</span>
        ),
      className: "whitespace-nowrap text-center",
    })),
    {
      id: "used-by",
      header: "Used by",
      sortable: true,
      sortValue: (item) => (usage ? (usage[item.id] ?? []).filter((u) => !u.unknown).length : null),
      cell: (item) => {
        if (usageError) return <span className="text-muted-foreground" title={usageError}>—</span>;
        if (usageLoading || !usage) return <span className="text-muted-foreground">…</span>;
        return <span data-testid="processor-row-usage" className="tabular-nums">{usageSummary(usage[item.id] ?? [])}</span>;
      },
      className: "whitespace-nowrap",
    },
    {
      id: "outcomes",
      header: "Outcomes (24 h)",
      sortable: true,
      align: "end",
      sortValue: (item) => outcomeCounts?.[item.id]?.count ?? null,
      cell: (item) => (
        <span data-testid="processor-row-outcomes" className="tabular-nums">
          {outcomeCounts ? formatOutcomeCount(outcomeCounts[item.id]) : "…"}
        </span>
      ),
      className: "whitespace-nowrap",
    },
  ], [outcomeCounts, usage, usageError, usageLoading]);

  return (
    <DataList
      label="Processor types"
      testId="processor-list"
      rows={items}
      columns={columns}
      getRowId={(item) => item.id}
      getRowHref={(item) => routes.processor(item.id)}
      getRowLabel={(item) => item.title}
      getRowTestId={() => "processor-row"}
      searchPlaceholder="Search processors"
      filterRow={(item, query) => {
        const q = query.toLowerCase();
        return [item.title, item.id, item.description].some((text) => text.toLowerCase().includes(q));
      }}
      loading={loading}
      empty={{ title: "No processor types are available." }}
    />
  );
}
