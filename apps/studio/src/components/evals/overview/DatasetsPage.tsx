"use client";

import React from "react";
import { Badge } from "@/components/ui/Badge";
import { PageHeader } from "@/components/frame/PageHeader";
import { DataList, type DataListColumn } from "@/components/shared/DataList";
import { StatusPill } from "@/components/runs/StatusPill";
import { listAllSuites, type OverviewSuite } from "@/lib/evalOverview";
import { formatRate, targetText } from "@/lib/evals";
import { navCrumb } from "@/lib/nav";
import { routes } from "@/lib/routes";
import type { RunStatus } from "@/lib/runs";
import { LoadMore } from "./LoadMore";
import { usePagedList } from "./usePagedList";

const columns: DataListColumn<OverviewSuite>[] = [
  {
    id: "name",
    header: "Dataset",
    sortable: true,
    sortValue: (s) => s.name,
    cell: (s) => <span className="truncate">{s.name}</span>,
  },
  {
    id: "target",
    header: "Target",
    sortable: true,
    sortValue: (s) => targetText({ kind: s.target_kind, name: s.target_name }),
    cell: (s) => (
      <span className="inline-flex min-w-0 items-center gap-2" data-testid="dataset-target">
        <span className="truncate">{targetText({ kind: s.target_kind, name: s.target_name ?? s.agent_name })}</span>
        {s.agent_archived ? <Badge variant="neutral">Archived</Badge> : null}
      </span>
    ),
  },
  { id: "cases", header: "Items", align: "end", sortable: true, sortValue: (s) => s.case_count, cell: (s) => s.case_count, className: "tabular-nums" },
  {
    id: "last-run",
    header: "Last run",
    sortable: true,
    sortValue: (s) => s.last_run?.created_at ?? "",
    cell: (s) =>
      s.last_run ? (
        <span className="inline-flex flex-wrap items-center gap-2" data-testid="dataset-last-run">
          <StatusPill status={s.last_run.display_status as RunStatus} />
          <span className="tabular-nums" title="Pass rate">{formatRate(s.last_run.aggregates.pass_rate)}</span>
        </span>
      ) : (
        <span className="text-muted-foreground" data-testid="dataset-last-run">Never run</span>
      ),
  },
  {
    id: "archived",
    header: <span className="sr-only">State</span>,
    className: "hidden md:table-cell",
    cell: (s) => (s.archived ? <Badge variant="neutral">Archived</Badge> : null),
  },
];

/** Datasets: every test suite of the tenant, across agents and workflows. A row opens the dataset. */
export function DatasetsPage() {
  const list = usePagedList((after, signal) => listAllSuites({ after, signal }), "Failed to load datasets");
  return (
    <>
      <PageHeader crumbs={[navCrumb("datasets", { current: true })]} />
      <div className="mx-auto flex w-full min-w-0 max-w-7xl flex-col gap-4 px-4 py-6">
        <p className="text-body text-muted-foreground">The test suites of every agent and workflow. Open one to edit its items, import a file and run it.</p>
        <DataList<OverviewSuite>
          label="Datasets"
          testId="datasets-list"
          rows={list.rows}
          columns={columns}
          getRowId={(s) => s.id}
          getRowHref={(s) => routes.dataset(s.id)}
          getRowLabel={(s) => `${s.name}, ${targetText({ kind: s.target_kind, name: s.target_name ?? s.agent_name })}, ${s.case_count} items`}
          getRowTestId={() => "dataset-row"}
          searchPlaceholder="Search by dataset or target"
          filterRow={(s, q) => `${s.name}\n${s.target_name ?? s.agent_name ?? ""}`.toLowerCase().includes(q.toLowerCase())}
          loading={list.loading}
          error={list.error}
          onRetry={list.reload}
          empty={{
            title: "No datasets yet",
            description: "Create a test suite in an agent's or a workflow's Tests tab. It appears here with its last result.",
          }}
        />
        <LoadMore list={list} label="datasets" />
      </div>
    </>
  );
}
