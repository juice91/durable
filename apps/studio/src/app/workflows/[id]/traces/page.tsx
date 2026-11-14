"use client";

import React, { Suspense, useCallback, useEffect, useRef, useState } from "react";
import { useParams } from "next/navigation";
import { StatusPill } from "@/components/runs/StatusPill";
import { DataList, type DataListColumn } from "@/components/shared/DataList";
import { TraceLink } from "@/components/traces/TraceLink";
import { TraceSheetHost } from "@/components/traces/TraceSheetHost";
import { Button } from "@/components/ui/Button";
import { WorkflowHeader } from "@/components/workflows/WorkflowHeader";
import { ApiError } from "@/lib/api";
import { formatCost, formatTokens } from "@/lib/format";
import { WORKFLOW_TRACE_TAB_PARAM } from "@/lib/routes/workflows";
import { formatDuration, runDurationMs, type TraceRun } from "@/lib/traces";
import { getWorkflow, listWorkflowRuns, type Workflow } from "@/lib/workflows";

const PAGE_SIZE = 50;
/** The row test id, as a `testId` key so the test-id inventory sees it rendered. */
const TRACE_ROW = { testId: "workflow-trace-row" } as const;
const dim = "text-muted-foreground";

/**
 * `/workflows/:id/traces` (SL.14): the workflow's runs with their trace columns. A row opens the
 * trace Sheet in place (`?trace=<runId>`, the Sheet's tab on `traceTab`), with prev / next over the
 * rows shown.
 */
function WorkflowTraces() {
  const params = useParams();
  const id = typeof params.id === "string" ? params.id : "";
  const [workflow, setWorkflow] = useState<Workflow | null>(null);
  const [notFound, setNotFound] = useState(false);
  const [runs, setRuns] = useState<TraceRun[]>([]);
  const [nextBefore, setNextBefore] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [visibleIds, setVisibleIds] = useState<string[] | null>(null);
  // Only the latest request (first page or "Load more") may write state.
  const requestRef = useRef(0);

  useEffect(() => {
    if (!id) return;
    let cancelled = false;
    getWorkflow(id)
      .then((wf) => {
        if (!cancelled) setWorkflow(wf);
      })
      .catch((reason: unknown) => {
        if (!cancelled && reason instanceof ApiError && reason.status === 404) setNotFound(true);
      });
    return () => {
      cancelled = true;
    };
  }, [id]);

  const load = useCallback(async (before?: string) => {
    if (!id) return;
    const request = ++requestRef.current;
    setLoading(true);
    if (!before) setError(null);
    try {
      const page = await listWorkflowRuns(id, { limit: PAGE_SIZE, before });
      if (request !== requestRef.current) return;
      // Same endpoint as the Traces list (GET /runs): the rows carry the usage columns too.
      const items = page.items as TraceRun[];
      setRuns((prev) => (before ? [...prev, ...items] : items));
      setNextBefore(page.next_before);
    } catch (reason) {
      if (request !== requestRef.current) return;
      setError(reason instanceof Error && reason.message ? reason.message : "Failed to load runs");
    } finally {
      if (request === requestRef.current) setLoading(false);
    }
  }, [id]);

  useEffect(() => {
    void load();
    return () => {
      requestRef.current += 1;
    };
  }, [load]);

  const columns: DataListColumn<TraceRun>[] = [
    {
      id: "time",
      header: "Started",
      sortable: true,
      sortValue: (run) => new Date(run.created_at).getTime(),
      cell: (run) => <span className="whitespace-nowrap font-mono text-caption">{new Date(run.created_at).toLocaleString()}</span>,
    },
    { id: "status", header: "Status", cell: (run) => <StatusPill status={run.display_status} /> },
    { id: "version", header: "Version", cell: (run) => (run.workflow_version != null ? `v${run.workflow_version}` : <span className={dim}>—</span>) },
    {
      id: "tokens",
      header: "Tokens (in / out)",
      align: "end",
      cell: (run) => (
        <span className="whitespace-nowrap font-mono text-caption">
          {run.input_tokens != null || run.output_tokens != null ? `${formatTokens(run.input_tokens)} / ${formatTokens(run.output_tokens)}` : <span className={dim}>-</span>}
        </span>
      ),
    },
    { id: "cost", header: "Cost", align: "end", cell: (run) => <span className="whitespace-nowrap font-mono text-caption">{formatCost(run.cost_usd) ?? <span className={dim}>-</span>}</span> },
    {
      id: "duration",
      header: "Duration",
      align: "end",
      sortable: true,
      sortValue: (run) => runDurationMs(run),
      cell: (run) => <span className={`whitespace-nowrap tabular-nums ${dim}`}>{formatDuration(runDurationMs(run))}</span>,
    },
    {
      id: "trace",
      header: "Trace",
      cell: (run) => (
        <TraceLink runId={run.run_id} data-testid="workflow-trace-open" className="text-info-text underline-offset-4 hover:underline">
          Open
        </TraceLink>
      ),
    },
  ];

  return (
    <TraceSheetHost tabParam={WORKFLOW_TRACE_TAB_PARAM} runIds={visibleIds ?? runs.map((run) => run.run_id)}>
      {(sheet) => (
        <div className="flex min-w-0 flex-col">
          <WorkflowHeader workflowId={id} name={workflow?.name ?? null} tab="traces" />
          <div className="flex min-w-0 flex-col gap-4 p-4">
            {notFound ? <p role="alert" data-testid="workflow-not-found" className="rounded-md border border-destructive/30 bg-destructive/10 p-3 text-body text-destructive-text">Workflow not found.</p> : null}
            <p className="text-label text-muted-foreground">This workflow&apos;s runs with their span tree, tokens and cost.</p>
            <DataList<TraceRun>
              label="Workflow runs"
              rows={runs}
              columns={columns}
              getRowId={(run) => run.run_id}
              onRowOpen={(run) => sheet.open(run.run_id)}
              getRowLabel={(run) => `Open the trace of the run started ${new Date(run.created_at).toLocaleString()}`}
              getRowTestId={() => TRACE_ROW.testId}
              selectedRowId={sheet.runId}
              onVisibleRowIdsChange={setVisibleIds}
              loading={loading}
              error={error}
              onRetry={() => void load()}
              empty={{ title: "No runs yet.", description: "Runs of this workflow appear here with their traces." }}
              testId="workflow-traces-list"
            />
            {nextBefore ? (
              <div className="flex justify-center">
                <Button variant="secondary" size="sm" onClick={() => void load(nextBefore)} disabled={loading}>
                  {loading ? "Loading..." : "Load more"}
                </Button>
              </div>
            ) : null}
          </div>
        </div>
      )}
    </TraceSheetHost>
  );
}

export default function WorkflowTracesPage() {
  // useSearchParams (the trace Sheet) needs a Suspense boundary for the static prerender.
  return (
    <Suspense fallback={<p className="p-4 text-muted-foreground">Loading runs…</p>}>
      <WorkflowTraces />
    </Suspense>
  );
}
