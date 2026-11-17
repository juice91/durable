"use client";

import React, { useCallback, useEffect, useState } from "react";
import Link from "next/link";
import { getWorkflowVersion, listWorkflowRuns, listWorkflows, type Workflow, type WorkflowSpec } from "@/lib/workflows";
import type { RunStatus } from "@/lib/runs";
import { routes } from "@/lib/routes";
import { PageHeader } from "@/components/frame/PageHeader";
import { DataList, type DataListColumn } from "@/components/shared/DataList";
import { Button } from "@/components/ui/Button";
import { navCrumb } from "@/lib/nav";

/** A run that is still executing. `waiting_approval` is pending input, not running. */
const RUNNING: ReadonlySet<RunStatus> = new Set(["queued", "running", "recovering"]);

interface Row extends Workflow {
  /** The latest published spec, once loaded (the step count comes from it, else the draft). */
  latestSpec: WorkflowSpec | null;
  /** Runs still queued or executing. Null until the run list returns. */
  running: number | null;
  /** Runs paused for a person (`waiting_approval`). Null until the run list returns. */
  pendingInput: number | null;
}

/** The row's test id (kept from P0), through a `testId` binding so the test-id inventory sees it rendered. */
function workflowRowTestId(id: string): string {
  const testId = `workflow-row-${id}`;
  return testId;
}

function shownSpec(row: Row): WorkflowSpec | null {
  return row.latestSpec ?? row.draft_spec ?? null;
}

/** A count cell: blank at zero, the way the workflow list leaves an idle column empty. */
function countCell(value: number | null): React.ReactNode {
  if (value == null || value === 0) return null;
  return <span className="tabular-nums">{value}</span>;
}

/**
 * `/workflows`: Name, Running, Pending input and Number of steps, matching the workflow tour.
 * A row opens the Graph tab. Schedules opens the cron list. Workflows are created through the API.
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
        setRows(workflows.map((w) => ({ ...w, latestSpec: null, running: null, pendingInput: null })));
        const details = await Promise.all(workflows.map(async (w) => {
          const version = w.latest_version == null
            ? null
            : await getWorkflowVersion(w.id, w.latest_version).catch(() => null);
          const runs = await listWorkflowRuns(w.id, { limit: 200 }).catch(() => null);
          const items = runs?.items ?? [];
          return {
            latestSpec: version?.spec ?? null,
            running: runs ? items.filter((run) => RUNNING.has(run.display_status)).length : null,
            pendingInput: runs ? items.filter((run) => run.display_status === "waiting_approval").length : null,
          };
        }));
        if (!cancelled) setRows(workflows.map((w, i) => ({ ...w, ...details[i] })));
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
      id: "running",
      header: "Running",
      align: "end",
      sortable: true,
      sortValue: (row) => row.running,
      cell: (row) => countCell(row.running),
    },
    {
      id: "pending",
      header: "Pending input",
      align: "end",
      sortable: true,
      sortValue: (row) => row.pendingInput,
      cell: (row) => countCell(row.pendingInput),
    },
    {
      id: "steps",
      header: "Number of steps",
      align: "end",
      sortable: true,
      sortValue: (row) => shownSpec(row)?.steps.length ?? null,
      cell: (row) => <span className="tabular-nums">{shownSpec(row)?.steps.length ?? "—"}</span>,
    },
  ];

  return (
    <>
      <PageHeader crumbs={[navCrumb("workflows", { current: true })]} />
      <div className="flex min-w-0 flex-col gap-4 p-4">
        <DataList<Row>
          label="Workflows"
          rows={rows ?? []}
          columns={columns}
          getRowId={(row) => row.id}
          getRowHref={(row) => routes.workflow(row.id)}
          getRowTestId={(row) => workflowRowTestId(row.id)}
          searchPlaceholder="Filter by name or description"
          toolbar={
            <Button variant="primary" size="md" asChild>
              <Link href={routes.workflowSchedules()} data-testid="workflows-schedules">Schedules</Link>
            </Button>
          }
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
