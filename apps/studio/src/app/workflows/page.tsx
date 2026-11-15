"use client";

import React, { useCallback, useEffect, useState } from "react";
import { formatDateTime } from "@/components/approvals/ApprovalCard";
import { getWorkflowVersion, listWorkflows, type Workflow, type WorkflowSpec } from "@/lib/workflows";
import { routes } from "@/lib/routes";
import { PageHeader } from "@/components/frame/PageHeader";
import { DataList, type DataListColumn } from "@/components/shared/DataList";
import { navCrumb } from "@/lib/nav";

interface Row extends Workflow {
  /** When the latest version was published (the API has no updated_at for a workflow). */
  updatedAt: string | null;
  /** The latest published spec, once loaded (steps and description come from it, else the draft). */
  latestSpec: WorkflowSpec | null;
}

/** The row's test id (kept from P0), through a `testId` binding so the test-id inventory sees it rendered. */
function workflowRowTestId(id: string): string {
  const testId = `workflow-row-${id}`;
  return testId;
}

function shownSpec(row: Row): WorkflowSpec | null {
  return row.latestSpec ?? row.draft_spec ?? null;
}

/**
 * `/workflows` (SL.14): a DataList of Name, Description, Steps, Latest version and Updated. A row
 * opens the workflow's Graph tab. Running and Waiting counts are not shown: no single existing call
 * returns them (open question 3). Workflows are created through the API, so there is no create
 * action.
 */
export default function WorkflowsPage() {
  const [rows, setRows] = useState<Row[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    let cancelled = false;
    setError(null);
    listWorkflows()
      .then(async (workflows) => {
        if (cancelled) return;
        setRows(workflows.map((w) => ({ ...w, updatedAt: null, latestSpec: null })));
        const published = await Promise.all(workflows.map(async (w) => {
          if (w.latest_version == null) return null;
          try {
            return await getWorkflowVersion(w.id, w.latest_version);
          } catch {
            return null;
          }
        }));
        if (!cancelled) setRows(workflows.map((w, i) => ({ ...w, updatedAt: published[i]?.created_at ?? null, latestSpec: published[i]?.spec ?? null })));
      })
      .catch((reason: unknown) => {
        if (!cancelled) setError(reason instanceof Error ? reason.message : "Failed to load workflows");
      });
    return () => {
      cancelled = true;
    };
  }, [attempt]);

  const retry = useCallback(() => setAttempt((n) => n + 1), []);

  const columns: DataListColumn<Row>[] = [
    { id: "name", header: "Name", sortable: true, sortValue: (row) => row.name.toLowerCase(), cell: (row) => row.name, className: "min-w-40" },
    {
      id: "description",
      header: "Description",
      cell: (row) => {
        const description = shownSpec(row)?.description;
        return description ? <span className="line-clamp-1 md:max-w-md text-muted-foreground">{description}</span> : <span className="text-muted-foreground">—</span>;
      },
    },
    {
      id: "steps",
      header: "Steps",
      align: "end",
      sortable: true,
      sortValue: (row) => shownSpec(row)?.steps.length ?? null,
      cell: (row) => <span className="tabular-nums">{shownSpec(row)?.steps.length ?? "—"}</span>,
    },
    {
      id: "version",
      header: "Latest version",
      sortable: true,
      sortValue: (row) => row.latest_version ?? null,
      cell: (row) => (row.latest_version != null ? `v${row.latest_version}` : <span className="text-muted-foreground">Draft only</span>),
    },
    {
      id: "updated",
      header: "Updated",
      sortable: true,
      sortValue: (row) => (row.updatedAt ? new Date(row.updatedAt).getTime() : null),
      cell: (row) => <span className="whitespace-nowrap text-muted-foreground" title="When the latest version was published">{row.updatedAt ? formatDateTime(row.updatedAt) : "—"}</span>,
    },
  ];

  return (
    <>
      <PageHeader crumbs={[navCrumb("workflows", { current: true })]} />
      <div className="flex min-w-0 flex-col gap-4 p-4">
        <p className="text-label text-muted-foreground">Published workflow definitions: open one to see its execution graph, run it and follow its runs.</p>
        <DataList<Row>
          label="Workflows"
          rows={rows ?? []}
          columns={columns}
          getRowId={(row) => row.id}
          getRowHref={(row) => routes.workflow(row.id)}
          getRowTestId={(row) => workflowRowTestId(row.id)}
          searchPlaceholder="Search workflows"
          filterRow={(row, query) => {
            const q = query.toLowerCase();
            return row.name.toLowerCase().includes(q) || (shownSpec(row)?.description ?? "").toLowerCase().includes(q);
          }}
          defaultSort={{ columnId: "name", direction: "asc" }}
          loading={rows === null && !error}
          error={error}
          onRetry={retry}
          empty={{
            title: "No workflows yet",
            description: "Create a workflow definition through the API (POST /api/v1/workflows) and publish a version to run it here.",
          }}
          testId="workflows-list"
        />
      </div>
    </>
  );
}
