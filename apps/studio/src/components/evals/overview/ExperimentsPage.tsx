"use client";

import React from "react";
import { PageHeader } from "@/components/frame/PageHeader";
import { DataList, type DataListColumn } from "@/components/shared/DataList";
import { StatusPill } from "@/components/runs/StatusPill";
import { listAllEvalRuns, type OverviewRun } from "@/lib/evalOverview";
import { formatRate, runTargetLabel, targetText } from "@/lib/evals";
import { formatCost, formatDuration, formatScore } from "@/lib/format";
import { navCrumb } from "@/lib/nav";
import { routes } from "@/lib/routes";
import type { RunStatus } from "@/lib/runs";
import { LoadMore } from "./LoadMore";
import { usePagedList } from "./usePagedList";

function scoresText(run: OverviewRun): string {
  const scores = Object.entries(run.aggregates?.scores_avg ?? {});
  if (scores.length === 0) return "—";
  return scores.map(([name, value]) => `${name} ${formatScore(value)}`).join(", ");
}

const columns: DataListColumn<OverviewRun>[] = [
  {
    id: "started",
    header: "Started",
    sortable: true,
    sortValue: (r) => r.created_at,
    cell: (r) => <span className="whitespace-nowrap">{new Date(r.created_at).toLocaleString()}</span>,
  },
  { id: "suite", header: "Dataset", sortable: true, sortValue: (r) => r.suite_name ?? "", cell: (r) => <span className="truncate">{r.suite_name ?? "Deleted dataset"}</span> },
  {
    id: "target",
    header: "Target",
    sortable: true,
    sortValue: (r) => targetText({ kind: r.target_kind, name: r.target_name ?? r.agent_name }),
    className: "hidden sm:table-cell",
    cell: (r) => <span className="truncate" data-testid="experiment-target">{targetText({ kind: r.target_kind, name: r.target_name ?? r.agent_name })}</span>,
  },
  { id: "version", header: "Version", className: "hidden md:table-cell", cell: (r) => (r.target_kind === "workflow" ? (typeof r.version === "number" ? `v${r.version}` : typeof r.workflow_version === "number" ? `v${r.workflow_version}` : "—") : runTargetLabel(r)) },
  { id: "status", header: "Status", cell: (r) => <StatusPill status={r.display_status as RunStatus} /> },
  { id: "pass", header: "Pass rate", align: "end", sortable: true, sortValue: (r) => r.aggregates?.pass_rate ?? null, cell: (r) => formatRate(r.aggregates?.pass_rate), className: "tabular-nums" },
  { id: "scores", header: "Scores", className: "hidden lg:table-cell max-w-xs", cell: (r) => <span className="block truncate" title={scoresText(r)} data-testid="experiment-scores">{scoresText(r)}</span> },
  { id: "median", header: "Median", align: "end", className: "hidden lg:table-cell tabular-nums", cell: (r) => formatDuration(r.aggregates?.duration_ms_p50) },
  { id: "cost", header: "Cost", align: "end", className: "hidden lg:table-cell tabular-nums", cell: (r) => formatCost(r.aggregates?.cost_usd) ?? "—" },
];

/** Experiments: the eval runs of every agent and workflow, newest first. A row opens an agent run in its Tests tab, a workflow run's suite in the workflow's. */
export function ExperimentsPage() {
  const list = usePagedList((before, signal) => listAllEvalRuns({ before, signal }), "Failed to load experiments");
  return (
    <>
      <PageHeader crumbs={[navCrumb("experiments", { current: true })]} />
      <div className="mx-auto flex w-full min-w-0 max-w-7xl flex-col gap-4 px-4 py-6">
        <p className="text-body text-muted-foreground">Eval runs across all agents and workflows, newest first. Open a run for its per-case results.</p>
        <DataList<OverviewRun>
          label="Experiments"
          testId="experiments-list"
          rows={list.rows}
          columns={columns}
          getRowId={(r) => r.run_id}
          getRowHref={(r) =>
            r.target_kind === "workflow"
              ? routes.workflowTests(r.workflow_id ?? r.target_id, { suite: r.suite_id, run: r.run_id })
              : routes.agentTests(r.agent_id ?? "", { suite: r.suite_id, run: r.run_id })
          }
          getRowLabel={(r) => `Eval run of ${r.suite_name ?? "a deleted dataset"} on ${new Date(r.created_at).toLocaleString()}`}
          getRowTestId={() => "experiment-row"}
          searchPlaceholder="Search by dataset or target"
          filterRow={(r, q) => `${r.suite_name ?? ""}\n${r.target_name ?? r.agent_name ?? ""}`.toLowerCase().includes(q.toLowerCase())}
          manualSort={false}
          loading={list.loading}
          error={list.error}
          onRetry={list.reload}
          empty={{
            title: "No experiments yet",
            description: "Run a test suite from an agent's or a workflow's Tests tab. Its results appear here.",
          }}
        />
        <LoadMore list={list} label="experiments" />
      </div>
    </>
  );
}
