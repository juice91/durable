"use client";

import React, { useCallback, useEffect, useRef, useState } from "react";
import Link from "next/link";
import { StatusPill } from "@/components/runs/StatusPill";
import { formatDateTime } from "./ApprovalCard";
import type { Run } from "@/lib/runs";
import { listWorkflowRuns, workflowRunPath } from "@/lib/workflows";
import { isTerminal } from "@/lib/workflowRunState";

/** "1.2s", "3m 04s", "2h 05m"; null when either end is missing. */
export function formatDuration(startIso: string | null | undefined, endIso: string | null | undefined): string | null {
  if (!startIso || !endIso) return null;
  const ms = new Date(endIso).getTime() - new Date(startIso).getTime();
  if (!Number.isFinite(ms) || ms < 0) return null;
  if (ms < 60_000) return `${(ms / 1000).toFixed(1)}s`;
  const totalSeconds = Math.round(ms / 1000);
  const minutes = Math.floor(totalSeconds / 60);
  if (minutes < 60) return `${minutes}m ${String(totalSeconds % 60).padStart(2, "0")}s`;
  return `${Math.floor(minutes / 60)}h ${String(minutes % 60).padStart(2, "0")}m`;
}

/** The runs of one workflow, newest first, each linking to its live execution view. */
export function WorkflowRunHistory({ workflowId, refreshKey = 0 }: { workflowId: string; refreshKey?: number }) {
  // Runs are stored with the workflow they were loaded for, so a page reused for another
  // workflow (/workflows/A -> /workflows/B) never lists A's runs under B.
  const [loaded, setLoaded] = useState<{ workflowId: string; runs: Run[] } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  // Every load (effect or Refresh button) takes a ticket; only the latest one may write state.
  const loadSeq = useRef(0);
  const runs = loaded && loaded.workflowId === workflowId ? loaded.runs : null;

  const load = useCallback(async () => {
    const ticket = ++loadSeq.current;
    const stale = () => ticket !== loadSeq.current;
    setLoading(true);
    try {
      const page = await listWorkflowRuns(workflowId);
      if (stale()) return;
      setLoaded({ workflowId, runs: page.items });
      setError(null);
    } catch (reason) {
      if (!stale()) setError(reason instanceof Error ? reason.message : "Failed to load runs");
    } finally {
      if (!stale()) setLoading(false);
    }
  }, [workflowId]);

  useEffect(() => {
    void load();
    return () => {
      // Invalidate whatever is in flight: the workflow changed, a refresh was requested, or the
      // history unmounted.
      loadSeq.current += 1;
    };
  }, [load, refreshKey]);

  return (
    <section aria-labelledby="workflow-runs-heading" data-testid="workflow-run-history" className="rounded-lg border border-border bg-card p-5">
      <div className="mb-3 flex items-center justify-between gap-2">
        <h2 id="workflow-runs-heading" className="font-semibold text-card-foreground">Run history</h2>
        <button type="button" onClick={() => void load()} disabled={loading} className="rounded-md border border-border px-3 py-1 text-xs text-foreground hover:bg-accent disabled:opacity-50">
          {loading ? "Refreshing…" : "Refresh"}
        </button>
      </div>
      {error ? <p role="alert" className="text-sm text-destructive">{error}</p> : null}
      {runs && runs.length === 0 ? <p className="text-sm text-muted-foreground">No runs yet.</p> : null}
      {runs && runs.length > 0 ? (
        <div className="overflow-x-auto">
          <table className="w-full text-left text-sm" aria-label="Workflow runs">
            <thead className="text-xs uppercase text-muted-foreground">
              <tr>
                <th scope="col" className="py-2 pr-3">Run</th>
                <th scope="col" className="py-2 pr-3">Status</th>
                <th scope="col" className="py-2 pr-3">Version</th>
                <th scope="col" className="py-2 pr-3">Started</th>
                <th scope="col" className="py-2">Duration</th>
              </tr>
            </thead>
            <tbody>
              {runs.map((run) => (
                <tr key={run.run_id} data-testid={`workflow-run-row-${run.run_id}`} data-status={run.display_status} className="border-t border-border">
                  <td className="py-2 pr-3">
                    <Link href={workflowRunPath(workflowId, run.run_id)} className="font-mono text-primary underline-offset-4 hover:underline">{run.run_id.slice(0, 8)}</Link>
                  </td>
                  <td className="py-2 pr-3"><StatusPill status={run.display_status} /></td>
                  <td className="py-2 pr-3">{run.workflow_version != null ? `v${run.workflow_version}` : "—"}</td>
                  <td className="py-2 pr-3 text-muted-foreground">{formatDateTime(run.created_at)}</td>
                  <td className="py-2 text-muted-foreground">
                    {isTerminal(run.display_status) ? formatDuration(run.created_at, run.updated_at) ?? "—" : "in progress"}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : null}
    </section>
  );
}
