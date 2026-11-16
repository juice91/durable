"use client";

import React, { useCallback, useEffect, useRef, useState } from "react";
import Link from "next/link";
import { StatusPill } from "@/components/runs/StatusPill";
import { formatDateTime } from "@/components/approvals/ApprovalCard";
import type { Run } from "@/lib/runs";
import { listWorkflowRuns } from "@/lib/workflows";
import { isTerminal } from "@/lib/workflowRunState";
import { RerunButton } from "./RerunButton";
import { routes } from "@/lib/routes";
import { durationBetween, formatDuration } from "@/lib/format";

/** A run's duration from its two timestamps in the shared format, or "—" when either is missing. */
export function formatRunDuration(startIso: string | null | undefined, endIso: string | null | undefined): string {
  const ms = durationBetween(startIso, endIso);
  return ms === null ? "—" : formatDuration(ms);
}


export interface WorkflowRunHistoryProps {
  workflowId: string;
  refreshKey?: number;
  /** Show at most this many runs (the Graph tab's Recent runs card: 5). Default: the whole first page. */
  limit?: number;
  /** The heading. Default "Run history". */
  title?: string;
  /** Below the table ("All runs" on the Recent runs card). */
  footer?: React.ReactNode;
  /** `card`: a bordered card (the Runs tab); `panel`: borderless, inside the floating panel. */
  variant?: "card" | "panel";
  /** Adds a "Run again" action per terminal run; called with the new run's id. */
  onRerunStarted?: (runId: string) => void;
}

/** The runs of one workflow, newest first, each linking to its live execution view. */
export function WorkflowRunHistory({ workflowId, refreshKey = 0, limit, title = "Run history", footer, variant = "card", onRerunStarted }: WorkflowRunHistoryProps) {
  // Runs are stored with the workflow they were loaded for, so a page reused for another
  // workflow (/workflows/A -> /workflows/B) never lists A's runs under B.
  const [loaded, setLoaded] = useState<{ workflowId: string; runs: Run[] } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  // Every load (effect or Refresh button) takes a ticket; only the latest one may write state.
  const loadSeq = useRef(0);
  const loadedRuns = loaded && loaded.workflowId === workflowId ? loaded.runs : null;
  const runs = loadedRuns && limit !== undefined ? loadedRuns.slice(0, limit) : loadedRuns;
  const panel = variant === "panel";

  const load = useCallback(async () => {
    const ticket = ++loadSeq.current;
    const stale = () => ticket !== loadSeq.current;
    setLoading(true);
    try {
      const page = await listWorkflowRuns(workflowId, limit !== undefined ? { limit } : undefined);
      if (stale()) return;
      setLoaded({ workflowId, runs: page.items });
      setError(null);
    } catch (reason) {
      if (!stale()) setError(reason instanceof Error ? reason.message : "Failed to load runs");
    } finally {
      if (!stale()) setLoading(false);
    }
  }, [workflowId, limit]);

  useEffect(() => {
    void load();
    return () => {
      // Invalidate whatever is in flight: the workflow changed, a refresh was requested, or the
      // history unmounted.
      loadSeq.current += 1;
    };
  }, [load, refreshKey]);

  return (
    <section
      aria-labelledby="workflow-runs-heading"
      data-testid="workflow-run-history"
      className={panel ? "min-w-0 border-b border-border p-4 last:border-b-0" : "min-w-0 rounded-lg border border-border bg-surface p-4"}
    >
      <div className="mb-3 flex min-w-0 items-center justify-between gap-2">
        <h2 id="workflow-runs-heading" className={panel ? "text-label font-semibold text-foreground" : "text-title font-semibold text-foreground"}>{title}</h2>
        <button type="button" onClick={() => void load()} disabled={loading} className="rounded-md border border-border px-2 py-0.5 text-caption text-foreground outline-none hover:bg-fill-hover focus-visible:ring-2 focus-visible:ring-ring disabled:opacity-50">
          {loading ? "Refreshing…" : "Refresh"}
        </button>
      </div>
      {error ? <p role="alert" className="text-sm text-destructive-text">{error}</p> : null}
      {runs && runs.length === 0 ? <p className="text-label text-muted-foreground">No runs yet.</p> : null}
      {runs && runs.length > 0 ? (
        <div className="overflow-x-auto">
          <table className="w-full text-left text-label" aria-label="Workflow runs">
            <thead className="text-caption text-muted-foreground">
              <tr>
                <th scope="col" className="py-2 pr-3 font-medium">Run</th>
                <th scope="col" className="py-2 pr-3 font-medium">Status</th>
                {panel ? null : <th scope="col" className="py-2 pr-3 font-medium">Version</th>}
                <th scope="col" className="py-2 pr-3 font-medium">Started</th>
                <th scope="col" className="py-2 pr-3 font-medium">Duration</th>
                {onRerunStarted ? <th scope="col" className="py-2 font-medium"><span className="sr-only">Actions</span></th> : null}
              </tr>
            </thead>
            <tbody>
              {runs.map((run) => (
                <tr key={run.run_id} data-testid={`workflow-run-row-${run.run_id}`} data-status={run.display_status} className="border-t border-border">
                  <td className="py-2 pr-3">
                    <Link href={routes.workflowRun(workflowId, run.run_id)} className="rounded-sm font-mono text-info-text underline-offset-4 outline-none hover:underline focus-visible:ring-2 focus-visible:ring-ring">{run.run_id.slice(0, 8)}</Link>
                  </td>
                  <td className="py-2 pr-3"><StatusPill status={run.display_status} /></td>
                  {panel ? null : <td className="py-2 pr-3">{run.workflow_version != null ? `v${run.workflow_version}` : "—"}</td>}
                  <td className="py-2 pr-3 text-muted-foreground">{formatDateTime(run.created_at)}</td>
                  <td className="whitespace-nowrap py-2 tabular-nums text-muted-foreground">
                    {isTerminal(run.display_status) ? formatRunDuration(run.created_at, run.updated_at) : "in progress"}
                  </td>
                  {onRerunStarted ? (
                    <td className="py-2">
                      {isTerminal(run.display_status) ? <RerunButton run={run} onStarted={onRerunStarted} /> : null}
                    </td>
                  ) : null}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : null}
      {footer ? <div className="mt-3">{footer}</div> : null}
    </section>
  );
}
