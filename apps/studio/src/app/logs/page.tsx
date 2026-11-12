"use client";

import React, { useState, useEffect, useCallback } from "react";
import { useRouter } from "next/navigation";
import { listRuns, type Run } from "@/lib/runs";
import { StatusPill } from "@/components/runs/StatusPill";
import { buildLogsLink, buildAllPlatformLogsLink } from "@/lib/grafana";

type FilterStatus = "all" | "queued" | "running" | "recovering" | "completed" | "failed" | "needs_attention";

export default function LogsPage() {
  const [runs, setRuns] = useState<Run[]>([]);
  const [nextBefore, setNextBefore] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [filter, setFilter] = useState<FilterStatus>("all");
  const router = useRouter();

  const loadRuns = useCallback(async (before?: string) => {
    setLoading(true);
    try {
      const result = await listRuns({
        limit: 50,
        before,
        status: filter === "all" ? undefined : filter,
      });
      setRuns((prev) => {
        const newRuns = before ? [...prev, ...result.items] : result.items;
        return newRuns.sort((a, b) => new Date(b.created_at).getTime() - new Date(a.created_at).getTime());
      });
      setNextBefore(result.next_before);
    } catch (error) {
      console.error("Failed to load runs:", error);
    } finally {
      setLoading(false);
    }
  }, [filter]);

  useEffect(() => {
    loadRuns();
  }, [loadRuns]);

  const handleViewRun = (runId: string) => {
    router.push(`/runs/${runId}`);
  };

  return (
    <div className="max-w-6xl mx-auto px-4 py-8">
      <div className="mb-6">
        <h1 className="text-2xl font-bold text-card-foreground">Logs</h1>
        <p className="text-muted-foreground mt-1">View application logs correlated by trace</p>
      </div>

      <div className="mb-4 flex items-center gap-2">
        <label htmlFor="status-filter" className="text-sm font-medium text-muted-foreground">
          Filter by status:
        </label>
        <select
          id="status-filter"
          value={filter}
          onChange={(e) => setFilter(e.target.value as FilterStatus)}
          className="rounded-md border border-input bg-background px-3 py-2 text-sm ring-offset-background file:border-0 file:bg-transparent file:text-sm file:font-medium placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 disabled:cursor-not-allowed disabled:opacity-50"
        >
          <option value="all">All</option>
          <option value="completed">Completed</option>
          <option value="failed">Failed</option>
          <option value="running">Running</option>
          <option value="queued">Queued</option>
          <option value="recovering">Recovering</option>
          <option value="needs_attention">Needs Attention</option>
        </select>
      </div>

      <div className="mb-4">
        <a
          href={buildAllPlatformLogsLink()}
          target="_blank"
          rel="noopener noreferrer"
          className="inline-flex items-center text-primary hover:underline"
        >
          View all platform logs
        </a>
      </div>

      {loading && runs.length === 0 ? (
        <div className="flex items-center justify-center py-12">
          <p className="text-muted-foreground">Loading runs...</p>
        </div>
      ) : (
        <>
          <div className="rounded-md border bg-card text-card-foreground shadow">
            <table className="w-full text-sm">
              <thead className="bg-muted/50">
                <tr>
                  <th className="px-4 py-3 text-left font-medium text-muted-foreground">Time</th>
                  <th className="px-4 py-3 text-left font-medium text-muted-foreground">Agent</th>
                  <th className="px-4 py-3 text-left font-medium text-muted-foreground">Status</th>
                  <th className="px-4 py-3 text-left font-medium text-muted-foreground">Conversation / Turn</th>
                  <th className="px-4 py-3 text-left font-medium text-muted-foreground">Duration</th>
                  <th className="px-4 py-3 text-left font-medium text-muted-foreground">Logs</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-border">
                {runs.map((run) => {
                  const duration = run.updated_at
                    ? new Date(run.updated_at).getTime() - new Date(run.created_at).getTime()
                    : null;
                  const hasTrace = !!run.trace_id;

                  return (
                    <tr key={run.run_id} className="hover:bg-muted/50 transition-colors">
                      <td className="px-4 py-3 font-mono text-xs">
                        {new Date(run.created_at).toLocaleString()}
                      </td>
                      <td className="px-4 py-3 font-medium">{run.agent_id}</td>
                      <td className="px-4 py-3">
                        <StatusPill status={run.display_status as any} />
                      </td>
                      <td className="px-4 py-3 text-muted-foreground">
                        {run.conversation_id && run.turn !== null ? (
                          <div className="flex flex-col gap-0.5">
                            <span>Conv: {run.conversation_id.slice(0, 8)}...</span>
                            <span className="text-xs">Turn: {run.turn}</span>
                          </div>
                        ) : (
                          <span className="text-muted-foreground/60">N/A</span>
                        )}
                      </td>
                      <td className="px-4 py-3 text-muted-foreground">
                        {duration !== null ? `${Math.round(duration / 1000)}s` : "-"}
                      </td>
                      <td className="px-4 py-3">
                        {hasTrace ? (
                          <a
                            href={buildLogsLink(run.trace_id!)}
                            target="_blank"
                            rel="noopener noreferrer"
                            className="inline-flex items-center text-primary hover:underline"
                          >
                            View
                          </a>
                        ) : (
                          <span
                            className="text-muted-foreground/50 cursor-not-allowed"
                            title="No trace recorded"
                          >
                            N/A
                          </span>
                        )}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>

          {nextBefore && (
            <div className="mt-4 flex justify-center">
              <button
                onClick={() => loadRuns(nextBefore)}
                disabled={loading}
                className="inline-flex items-center justify-center rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground hover:bg-primary/90 disabled:opacity-50 disabled:cursor-not-allowed"
              >
                {loading ? "Loading..." : "Load more"}
              </button>
            </div>
          )}
        </>
      )}
    </div>
  );
}
