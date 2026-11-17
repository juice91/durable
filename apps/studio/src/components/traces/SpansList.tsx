"use client";

import React, { useCallback, useEffect, useRef, useState } from "react";
import { listSpans, type SpanFilters, type SpanRow } from "@/lib/spans";
import { KIND_LABELS, formatDuration, hasActiveFilters } from "@/lib/traces";
import { Badge } from "@/components/ui/Badge";
import { DataList, type DataListColumn } from "@/components/shared/DataList";
import { Button } from "@/components/ui/Button";
import { TraceLink } from "./TraceLink";

export interface SpansListProps {
  filters: SpanFilters;
  /** A row was opened: the trace Sheet at this run and span. */
  onOpen: (runId: string, spanId: string) => void;
  /** The open trace Sheet's run and span: that row is highlighted. */
  selectedRunId?: string | null;
  selectedSpanId?: string | null;
}

const dim = "text-muted-foreground";

/** The flat list of spans (GET /spans), newest first, paged with the API's opaque cursor. */
export function SpansList({ filters, onOpen, selectedRunId = null, selectedSpanId = null }: SpansListProps) {
  const [rows, setRows] = useState<SpanRow[]>([]);
  const [nextBefore, setNextBefore] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [reloading, setReloading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // A response applies only while it is the latest request's (filters change, slow pages).
  const requestRef = useRef(0);
  const filterKey = JSON.stringify(filters);

  const load = useCallback(async (before?: string) => {
    const request = ++requestRef.current;
    setLoading(true);
    if (!before) {
      setNextBefore(null);
      setError(null);
      setReloading(true);
    }
    try {
      const result = await listSpans(JSON.parse(filterKey) as SpanFilters, { limit: 50, before });
      if (request !== requestRef.current) return;
      setRows((prev) => (before ? [...prev, ...result.items] : result.items));
      setNextBefore(result.next_before);
    } catch (err) {
      if (request !== requestRef.current) return;
      setError(err instanceof Error && err.message ? err.message : "Failed to load spans");
      if (!before) {
        setRows([]);
        setNextBefore(null);
      }
    } finally {
      if (request === requestRef.current) {
        setLoading(false);
        setReloading(false);
      }
    }
  }, [filterKey]);

  useEffect(() => {
    void load();
  }, [load]);

  const columns: DataListColumn<SpanRow>[] = [
    {
      id: "start",
      header: "Start",
      cell: (row) => <span className="whitespace-nowrap font-mono text-caption">{row.start ? new Date(row.start).toLocaleString() : "-"}</span>,
    },
    { id: "kind", header: "Kind", cell: (row) => KIND_LABELS[row.kind] ?? row.kind },
    {
      id: "name",
      header: "Name",
      cell: (row) => (
        <span className="min-w-0">
          <span className="font-medium">{row.name}</span>
          {row.start && row.end ? <span className={`ml-2 ${dim}`}>{formatDuration(Math.max(0, new Date(row.end).getTime() - new Date(row.start).getTime()))}</span> : null}
        </span>
      ),
    },
    {
      id: "run",
      header: "Run",
      cell: (row) => (
        <TraceLink runId={row.run_id} className="font-mono text-caption text-info-text underline-offset-4 hover:underline" onClick={(event) => event.stopPropagation()}>
          {row.run_id.slice(0, 8)}
        </TraceLink>
      ),
    },
    { id: "status", header: "Status", cell: (row) => <Badge variant={row.status === "ok" ? "success" : row.status === "error" ? "destructive" : "neutral"}>{row.status}</Badge> },
  ];

  if (loading && rows.length === 0 && !error) return <p className="py-12 text-center text-muted-foreground">Loading spans...</p>;

  return (
    <>
      {error ? (
        <div role="alert" className="rounded-md border border-destructive/40 bg-destructive/10 p-3 text-sm text-destructive-text">{error}</div>
      ) : null}
      <div aria-busy={reloading} className={`min-w-0 transition-opacity motion-reduce:transition-none ${reloading ? "pointer-events-none opacity-50" : ""}`}>
        <DataList<SpanRow>
          label="Spans"
          rows={rows}
          columns={columns}
          getRowId={(row) => `${row.run_id}:${row.span_id}`}
          onRowOpen={(row) => onOpen(row.run_id, row.span_id)}
          getRowLabel={(row) => `Open ${row.name} in the trace of run ${row.run_id.slice(0, 8)}`}
          getRowTestId={() => "span-row"}
          selectedRowId={selectedRunId && selectedSpanId ? `${selectedRunId}:${selectedSpanId}` : null}
          empty={{ title: hasActiveFilters(filters) || filters.span_kind ? "No spans match these filters." : "No spans yet. Spans appear when a run finishes." }}
          testId="spans-list"
        />
      </div>
      {nextBefore ? (
        <div className="flex justify-center">
          <Button variant="secondary" size="sm" onClick={() => load(nextBefore)} disabled={loading}>
            {loading ? "Loading..." : "Load more"}
          </Button>
        </div>
      ) : null}
    </>
  );
}
