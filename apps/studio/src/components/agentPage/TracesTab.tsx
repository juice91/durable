"use client";

import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import Link from "next/link";
import { formatCost, formatTokens } from "@/lib/format";
import { routes } from "@/lib/routes";
import { EMPTY_FILTERS, formatDuration, listTraceRuns, runDurationMs, traceFiltersToParams, type TraceRun } from "@/lib/traces";
import { DataList, type DataListColumn } from "@/components/shared/DataList";
import { StatusPill } from "@/components/runs/StatusPill";
import { Button } from "@/components/ui/Button";

const PAGE_SIZE = 50;
const dim = "text-muted-foreground";

/**
 * The agent page's Traces tab (SL.6): the agent's runs, newest first, each row opening its trace as
 * the Traces list does. "All filters" opens the full Traces list narrowed to this agent. SL.12 turns
 * the rows into the trace drawer.
 */
export function TracesTab({ agentId }: { agentId: string }) {
  const filters = useMemo(() => ({ ...EMPTY_FILTERS, agent_id: agentId }), [agentId]);
  const [runs, setRuns] = useState<TraceRun[]>([]);
  const [nextBefore, setNextBefore] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const [error, setError] = useState<string | null>(null);
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
        <span className="font-mono text-caption">
          {run.model_id ?? <span className={dim}>-</span>}
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
  ];

  return (
    <div data-testid="agent-traces-tab" className="flex min-w-0 flex-col gap-3 p-4">
      <DataList<TraceRun>
        label="Agent runs"
        rows={runs}
        columns={columns}
        getRowId={(run) => run.run_id}
        getRowHref={(run) => routes.trace(run.run_id)}
        getRowLabel={(run) => `Run started ${new Date(run.created_at).toLocaleString()}`}
        getRowTestId={() => "agent-trace-row"}
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
  );
}
