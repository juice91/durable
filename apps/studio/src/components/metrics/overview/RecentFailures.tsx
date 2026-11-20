"use client";

import React from "react";
import Link from "next/link";
import { StatusPill } from "@/components/runs/StatusPill";
import { RUN_KIND_LABELS, runKind } from "@/components/runs/RunSubject";
import { formatCount } from "@/lib/metrics";
import type { TraceRun } from "@/lib/traces";
import type { Run } from "@/lib/runs";
import { routes } from "@/lib/routes";

function short(id: string): string {
  return id.length > 8 ? `${id.slice(0, 8)}...` : id;
}

/** A plain-text run label (the row is one link, so no nested links): "Support bot",
 * "Workflow: Intake", "Tool: http.get", "Agent 1a2b3c4d...", "Test run: <target>" for evals,
 * or "KB source sync: <kb name>" for source sync runs. */
export function runLabel(
  run: Pick<Run, "kind" | "agent_id" | "tool_id" | "workflow_id" | "suite_name" | "knowledge_base_id" | "source_name" | "run_id">,
  agentNames: Record<string, string> = {},
  workflowNames: Record<string, string> = {},
): string {
  const kind = runKind(run);
  if (kind === "workflow") {
    const id = run.workflow_id ?? null;
    return `Workflow: ${id ? (workflowNames[id] ?? short(id)) : "unknown"}`;
  }
  if (kind === "tool") return `Tool: ${run.tool_id ?? "unknown"}`;
  if (kind === "eval") {
    // An eval run: target is a workflow or agent; include suite name if available.
    const id = run.workflow_id ?? run.agent_id ?? null;
    const names = run.workflow_id ? workflowNames : agentNames;
    const prefix = run.workflow_id ? "Workflow" : "Agent";
    if (!id) {
      return run.suite_name ? `Test run · ${run.suite_name}` : "Test run";
    }
    // Target is the name from the map (like "Triage") or "Workflow <short id>" when not found.
    const name = names[id] ?? short(id);
    const target = names[id] ? name : `${prefix} ${short(id)}`;
    // Base label is "Test run: <target>"; append " · <suite name>" if available.
    const baseLabel = `Test run: ${target}`;
    return run.suite_name ? `${baseLabel} · ${run.suite_name}` : baseLabel;
  }
  if (kind === "kb_source_sync") {
    // A knowledge-base source sync run (0029_kb_sources): show source name if available.
    // The source_name comes from the run's input JSON (parsed by the client).
    if (run.source_name) {
      return `${RUN_KIND_LABELS.kb_source_sync} (${run.source_name})`;
    }
    // Fallback to short run id when source_name is missing.
    return `${RUN_KIND_LABELS.kb_source_sync} ${short(run.run_id)}`;
  }
  if (!run.agent_id) return "Agent: unknown";
  return agentNames[run.agent_id] ?? `Agent ${short(run.agent_id)}`;
}

export interface RecentFailuresProps {
  /** Newest first (GET /runs with status=failed,needs_attention and the Metrics page's range and filters). */
  runs: readonly TraceRun[] | null;
  /** Failed plus needs-attention runs in the period, from the metrics summary; null while unknown. */
  total: number | null;
  /** The Traces list with the same range and statuses. */
  viewAllHref: string;
  agentNames?: Record<string, string>;
  workflowNames?: Record<string, string>;
  loading?: boolean;
  error?: string | null;
  /** The title's heading level: 2 on its own, 3 inside the Metrics Overview section. */
  headingLevel?: 2 | 3;
  /**
   * The Metrics page's operation filter (its label), or null. Runs are not split by operation (the
   * runs API has no such filter), so the panel then lists nothing and says why; the link goes to
   * every failed run of the period, without a count.
   */
  operation?: string | null;
}

/** "Needs attention" in the Metrics Overview: the newest failed and needs-attention runs of the
 * period; each row opens its trace. */
export function RecentFailures({
  runs,
  total,
  viewAllHref,
  agentNames = {},
  workflowNames = {},
  loading = false,
  error = null,
  headingLevel = 2,
  operation = null,
}: RecentFailuresProps) {
  const Heading = headingLevel === 3 ? "h3" : "h2";
  const viewAll = operation ? "View failed runs in Traces" : total === null ? "View all in Traces" : `View all ${formatCount(total)} in Traces`;
  return (
    <section
      className="min-w-0 rounded-lg border border-border bg-card text-card-foreground"
      aria-labelledby="recent-failures-title"
      aria-busy={loading || undefined}
      data-testid="recent-failures"
    >
      <div className="flex flex-wrap items-center justify-between gap-2 px-4 pt-4 pb-2">
        <Heading id="recent-failures-title" className="text-sm font-semibold text-foreground">
          Needs attention
        </Heading>
        <Link
          href={viewAllHref}
          className="rounded-sm text-xs text-primary hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          data-testid="recent-failures-all"
        >
          {viewAll}
        </Link>
      </div>
      <p className="px-4 pb-2 text-xs text-muted-foreground">Failed runs and runs that need attention, newest first.</p>
      {operation ? (
        <p className="px-4 pb-6 pt-2 text-sm text-muted-foreground" data-testid="recent-failures-operation">
          Failed runs are not split by operation, so none are listed while the {operation} filter is on. Clear it to see
          them, or open them in Traces.
        </p>
      ) : error ? (
        <p role="alert" className="mx-4 mb-4 rounded-md border border-destructive bg-destructive/10 p-3 text-sm text-foreground">
          {error}
        </p>
      ) : loading && !runs ? (
        <div className="mx-4 mb-4 h-32 rounded-md bg-muted animate-pulse" aria-label="Loading runs that need attention" />
      ) : !runs || runs.length === 0 ? (
        <p className="px-4 pb-6 pt-2 text-sm text-muted-foreground" data-testid="recent-failures-empty">
          No failed or needs-attention runs in this period.
        </p>
      ) : (
        <ul className="divide-y divide-border">
          {runs.map((run) => {
            const label = runLabel(run, agentNames, workflowNames);
            return (
              <li key={run.run_id}>
                <Link
                  href={routes.trace(run.run_id)}
                  className="flex flex-col gap-1 px-4 py-2.5 hover:bg-muted/50 transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-ring"
                  data-testid="recent-failure"
                >
                  <div className="flex items-center justify-between gap-2 min-w-0">
                    <span className="truncate text-sm font-medium text-foreground">{label}</span>
                    <StatusPill status={run.display_status} />
                  </div>
                  <div className="flex items-center justify-between gap-2 min-w-0 text-xs text-muted-foreground">
                    <span className="truncate" title={run.error ?? undefined}>
                      {run.error ?? "No error message"}
                    </span>
                    <time dateTime={run.created_at} className="shrink-0 font-mono">
                      {new Date(run.created_at).toLocaleString()}
                    </time>
                  </div>
                </Link>
              </li>
            );
          })}
        </ul>
      )}
    </section>
  );
}
