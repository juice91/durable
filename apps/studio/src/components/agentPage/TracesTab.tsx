"use client";

import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import Link from "next/link";
import { formatCost, formatTokens } from "@/lib/format";
import { routes } from "@/lib/routes";
import { EMPTY_FILTERS, formatDuration, listTraceRuns, runDurationMs, traceFiltersToParams, type TraceRun } from "@/lib/traces";
import { DataList, type DataListColumn } from "@/components/shared/DataList";
import { StatusPill } from "@/components/runs/StatusPill";
import { Button } from "@/components/ui/Button";
import { RunModel } from "@/components/traces/RunModel";
import { TraceLink } from "@/components/traces/TraceLink";
import { TraceSheetHost } from "@/components/traces/TraceSheetHost";

const PAGE_SIZE = 50;
/** The row's test id, as a `testId` key so the test-id inventory sees it rendered. */
const AGENT_TRACE_ROW = { testId: "agent-trace-row" } as const;
const dim = "text-muted-foreground";

/**
 * The agent page's Traces tab (SL.6, SL.12): the agent's runs, newest first; a row opens its trace
 * in the trace Sheet in place (`?trace=` on this page, so it survives a reload), with prev / next
 * through the rows. "Open in Traces" opens the full Traces list narrowed to this agent.
 */
export function TracesTab({ agentId }: { agentId: string }) {
  const filters = useMemo(() => ({ ...EMPTY_FILTERS, agent_id: agentId }), [agentId]);
  const [runs, setRuns] = useState<TraceRun[]>([]);
  const [nextBefore, setNextBefore] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // The rows' ids as the list shows them (the user's sort), for the Sheet's prev / next.
  const [visibleIds, setVisibleIds] = useState<string[] | null>(null);
  // A response applies only while its request is the latest (a retry or an agent switch wins).
  const requestRef = useRef(0);

  const load = useCallback(
    async (before?: string) => {
      const request = ++requestRef.current;
      if (before) setLoadingMore(true);
      else {
        setLoading(true);
        setError(null);
      }
      try {
        const page = await listTraceRuns(filters, { limit: PAGE_SIZE, before });
        if (request !== requestRef.current) return;
        setRuns((current) => (before ? [...current, ...page.items] : page.items));
        setNextBefore(page.next_before);
      } catch (reason) {
        if (request !== requestRef.current) return;
        setError(reason instanceof Error && reason.message ? reason.message : "Failed to load runs");
      } finally {
        if (request === requestRef.current) {
          setLoading(false);
          setLoadingMore(false);
        }
      }
    },
    [filters],
  );

  useEffect(() => {
    void load();
  }, [load]);

  const columns: DataListColumn<TraceRun>[] = [
    {
      id: "started",
      header: "Started",
      cell: (run) => <span className="font-mono text-caption">{new Date(run.created_at).toLocaleString()}</span>,
      sortable: true,
      sortValue: (run) => new Date(run.created_at).getTime(),
    },
    { id: "status", header: "Status", cell: (run) => <StatusPill status={run.display_status} /> },
    {
      id: "model",
      header: "Model",
      className: "hidden md:table-cell",
      headerClassName: "hidden md:table-cell",
      cell: (run) => (
        <span className="font-mono text-caption" data-testid="agent-trace-row-model">
          <RunModel run={run} />
          {run.model_overridden ? <span className={`ml-1 ${dim}`}>(override)</span> : null}
        </span>
      ),
    },
    {
      id: "version",
      header: "Version",
      className: "hidden md:table-cell",
      headerClassName: "hidden md:table-cell",
      cell: (run) => (run.draft ? "Draft" : run.version !== null && run.version !== undefined ? `v${run.version}` : <span className={dim}>-</span>),
    },
    {
      id: "tokens",
      header: "Tokens (in / out)",
      align: "end",
      className: "hidden lg:table-cell",
      headerClassName: "hidden lg:table-cell",
      cell: (run) =>
        run.input_tokens != null || run.output_tokens != null ? (
          <span className="font-mono text-caption">{`${formatTokens(run.input_tokens)} / ${formatTokens(run.output_tokens)}`}</span>
        ) : (
          <span className={dim}>-</span>
        ),
    },
    {
      id: "cost",
      header: "Cost",
      align: "end",
      className: "hidden lg:table-cell",
      headerClassName: "hidden lg:table-cell",
      cell: (run) => <span className="font-mono text-caption">{formatCost(run.cost_usd) ?? <span className={dim}>-</span>}</span>,
    },
    {
      id: "duration",
      header: "Duration",
      align: "end",
      cell: (run) => <span className="tabular-nums">{formatDuration(runDurationMs(run))}</span>,
      sortable: true,
      sortValue: (run) => runDurationMs(run),
    },
    {
      id: "conversation",
      header: "Conversation",
      className: "hidden md:table-cell",
      headerClassName: "hidden md:table-cell",
      cell: (run) =>
        run.conversation_id ? (
          <Link
            href={routes.agentChat(agentId, { conversation: run.conversation_id })}
            className="text-caption text-info-text hover:underline"
            data-testid="agent-trace-conversation"
          >
            Turn {run.turn ?? "?"}
          </Link>
        ) : (
          <span className={dim}>-</span>
        ),
    },
    {
      id: "trace",
      header: "Trace",
      // The same Sheet as the row, as a real link (new tab, copy link address).
      cell: (run) => (
        <TraceLink runId={run.run_id} className="text-caption text-info-text hover:underline" data-testid="agent-trace-open">
          Open
        </TraceLink>
      ),
    },
  ];

  return (
    <TraceSheetHost runIds={visibleIds ?? runs.map((run) => run.run_id)}>
      {(sheet) => (
        <div data-testid="agent-traces-tab" className="flex min-w-0 flex-col gap-3 p-4">
          <DataList<TraceRun>
            label="Agent runs"
            rows={runs}
            columns={columns}
            getRowId={(run) => run.run_id}
            onRowOpen={(run) => sheet.open(run.run_id)}
            getRowLabel={(run) => `Run started ${new Date(run.created_at).toLocaleString()}`}
            getRowTestId={() => AGENT_TRACE_ROW.testId}
            selectedRowId={sheet.runId}
            onVisibleRowIdsChange={setVisibleIds}
            defaultSort={{ columnId: "started", direction: "desc" }}
            loading={loading}
            error={error}
            onRetry={() => void load()}
            empty={{ title: "No runs yet", description: "Runs appear here once the agent is used in chat or by a workflow." }}
            toolbar={
              <Link href={routes.traces(traceFiltersToParams(filters))} className="text-label text-info-text hover:underline" data-testid="agent-traces-all">
                Open in Traces
              </Link>
            }
            testId="agent-traces-list"
          />
          {nextBefore && !loading ? (
            <div className="flex justify-center">
              <Button variant="secondary" size="sm" onClick={() => void load(nextBefore)} disabled={loadingMore}>
                {loadingMore ? "Loading…" : "Load more"}
              </Button>
            </div>
          ) : null}
        </div>
      )}
    </TraceSheetHost>
  );
}
