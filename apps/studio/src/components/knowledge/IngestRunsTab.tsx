"use client";

import React, { useCallback, useEffect, useRef, useState } from "react";
import { StatusPill } from "@/components/runs/StatusPill";
import { DataList, type DataListColumn } from "@/components/shared/DataList";
import { TraceLink, useTraceSheet } from "@/components/traces/TraceLink";
import { Button } from "@/components/ui/Button";
import { durationBetween, formatCost, formatDuration, formatTokens } from "@/lib/format";
import { listKnowledgeBaseRuns, type KnowledgeDocument } from "@/lib/knowledge";
import type { Run, RunStatus } from "@/lib/runs";
import { formatWhen } from "./knowledgeUi";

// DataList renders this on each row as data-testid (named `testId` so the test-id inventory sees it).
const RUN_ROW = { testId: "knowledge-run-row" } as const;

const TERMINAL: ReadonlySet<string> = new Set(["completed", "failed", "needs_attention"]);

/** What a kb_ingest run did, from its `input`: `{reindex: generation}` or `{document_id, doc_version}`. */
export function ingestRunMode(run: Pick<Run, "input">, documentTitles: ReadonlyMap<string, string> = new Map()): string {
  try {
    const input = JSON.parse(run.input) as { reindex?: unknown; document_id?: unknown; doc_version?: unknown };
    if (typeof input.reindex === "number") return `Reindex (generation ${input.reindex})`;
    if (typeof input.document_id === "string") {
      const title = documentTitles.get(input.document_id);
      const version = typeof input.doc_version === "number" ? ` v${input.doc_version}` : "";
      return `${title ?? "Document"}${version}`;
    }
  } catch {
    // Not JSON: fall through.
  }
  return "Ingest";
}

/** A kb_ingest run as GET /runs lists it: the run, plus its usage totals (null until it finishes). */
export type IngestRun = Run & { input_tokens?: number | null; cost_usd?: string | null };

/** What a finished kb_ingest run reports: sections (from its output) and embedding tokens and cost. */
export interface IngestRunTotals {
  /** The sections the knowledge base (or document) has after the run; null when not reported. */
  sections: number | null;
  /** Embedding tokens sent (the run's usage, else the output's approximate count); null when none. */
  tokens: number | null;
  /** The run's embedding cost (a decimal string); null when not recorded or unpriced. */
  costUsd: string | null;
}

/** The totals of a kb_ingest run (all null while it runs, or when it failed before reporting). */
export function ingestRunTotals(run: Pick<IngestRun, "output" | "input_tokens" | "cost_usd">): IngestRunTotals {
  let output: { chunk_count?: unknown; approx_tokens?: unknown } = {};
  try {
    if (run.output) output = JSON.parse(run.output) ?? {};
  } catch {
    // Not JSON: no sections reported.
  }
  const sections = typeof output.chunk_count === "number" ? output.chunk_count : null;
  const tokens = typeof run.input_tokens === "number" ? run.input_tokens : typeof output.approx_tokens === "number" ? output.approx_tokens : null;
  return { sections, tokens, costUsd: run.cost_usd ?? null };
}

export interface IngestRunsTabProps {
  kbId: string;
  /** Loaded documents, to name each run's document. */
  documents: readonly KnowledgeDocument[];
  /** Changes when the page knows of a new run (an upload, a retry, a reindex). */
  refreshKey?: number;
}

/** The knowledge base's kb_ingest runs (ingests and reindexes), newest first; a row opens its trace. */
export function IngestRunsTab({ kbId, documents, refreshKey = 0 }: IngestRunsTabProps) {
  const sheet = useTraceSheet();
  const [runs, setRuns] = useState<IngestRun[]>([]);
  const [nextBefore, setNextBefore] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const seq = useRef(0);
  const titles = new Map(documents.map((doc) => [doc.id, doc.title]));

  const load = useCallback(
    async (before: string | null = null) => {
      const ticket = before ? seq.current : ++seq.current;
      if (before) setLoadingMore(true);
      else setLoading(true);
      try {
        const page = await listKnowledgeBaseRuns(kbId, { before });
        if (ticket !== seq.current) return;
        setRuns((current) => (before ? [...current, ...page.items.filter((run) => !current.some((other) => other.run_id === run.run_id))] : page.items));
        setNextBefore(page.next_before);
        setError(null);
      } catch (reason: unknown) {
        if (ticket === seq.current) setError(reason instanceof Error ? reason.message : "Failed to load the ingest runs");
      } finally {
        if (ticket === seq.current) {
          setLoading(false);
          setLoadingMore(false);
        }
      }
    },
    [kbId],
  );

  useEffect(() => {
    void load();
    return () => {
      seq.current += 1;
    };
  }, [load, refreshKey]);

  const setRunIds = sheet?.setRunIds;
  const onVisible = useCallback((ids: string[]) => setRunIds?.(ids), [setRunIds]);

  const columns: DataListColumn<IngestRun>[] = [
    {
      id: "run",
      header: "Run",
      cell: (run) => <span className="font-mono">{run.run_id.slice(0, 8)}</span>,
    },
    {
      id: "mode",
      header: "What",
      className: "min-w-0 max-w-xs",
      cell: (run) => <span className="block truncate" data-testid="knowledge-run-mode">{ingestRunMode(run, titles)}</span>,
    },
    {
      id: "status",
      header: "Status",
      sortable: true,
      sortValue: (run) => run.display_status,
      cell: (run) => <StatusPill status={run.display_status as RunStatus} />,
    },
    {
      id: "started",
      header: "Started",
      sortable: true,
      sortValue: (run) => run.created_at,
      className: "hidden whitespace-nowrap md:table-cell",
      cell: (run) => <span className="text-caption text-muted-foreground">{formatWhen(run.created_at)}</span>,
    },
    {
      id: "duration",
      header: "Duration",
      align: "end",
      className: "hidden whitespace-nowrap sm:table-cell",
      cell: (run) => (
        <span className="text-caption tabular-nums text-muted-foreground">
          {TERMINAL.has(run.display_status) ? formatDuration(durationBetween(run.created_at, run.updated_at)) : "in progress"}
        </span>
      ),
    },
    {
      id: "sections",
      header: "Sections",
      align: "end",
      className: "hidden whitespace-nowrap sm:table-cell",
      cell: (run) => {
        const { sections } = ingestRunTotals(run);
        return (
          <span className="text-caption tabular-nums text-muted-foreground" data-testid="knowledge-run-sections">
            {sections === null ? "—" : sections.toLocaleString("en-US")}
          </span>
        );
      },
    },
    {
      id: "tokens",
      header: "Tokens",
      align: "end",
      className: "hidden whitespace-nowrap lg:table-cell",
      cell: (run) => {
        const { tokens } = ingestRunTotals(run);
        return (
          <span className="text-caption tabular-nums text-muted-foreground" data-testid="knowledge-run-tokens">
            {tokens === null ? "—" : formatTokens(tokens)}
          </span>
        );
      },
    },
    {
      id: "cost",
      header: "Cost",
      align: "end",
      className: "hidden whitespace-nowrap md:table-cell",
      cell: (run) => (
        <span className="text-caption tabular-nums text-muted-foreground" data-testid="knowledge-run-cost">
          {formatCost(ingestRunTotals(run).costUsd) ?? "—"}
        </span>
      ),
    },
    {
      id: "trace",
      header: "Trace",
      cell: (run) => (
        <TraceLink runId={run.run_id} className="text-caption text-info-text hover:underline" data-testid="knowledge-run-trace">
          Open
        </TraceLink>
      ),
    },
  ];

  return (
    <div className="flex min-w-0 flex-col gap-3" data-testid="knowledge-runs-tab">
      <DataList
        label="Ingest runs"
        testId="knowledge-runs"
        rows={runs}
        columns={columns}
        getRowId={(run) => run.run_id}
        onRowOpen={(run) => sheet?.open(run.run_id)}
        getRowLabel={(run) => `${ingestRunMode(run, titles)}, ${run.display_status}, started ${formatWhen(run.created_at)}`}
        getRowTestId={() => RUN_ROW.testId}
        selectedRowId={sheet?.runId ?? null}
        onVisibleRowIdsChange={onVisible}
        loading={loading}
        error={error}
        onRetry={() => void load()}
        toolbar={<Button size="sm" variant="secondary" onClick={() => void load()} disabled={loading} data-testid="knowledge-runs-refresh">Refresh</Button>}
        empty={{ title: "No ingest runs yet", description: "Each upload, retry and reindex runs here, with its progress, sections and embedding cost." }}
      />
      {nextBefore && !loading ? (
        <div className="flex justify-center">
          <Button variant="secondary" size="sm" onClick={() => void load(nextBefore)} disabled={loadingMore} data-testid="knowledge-runs-more">
            {loadingMore ? "Loading…" : "Load more"}
          </Button>
        </div>
      ) : null}
    </div>
  );
}
