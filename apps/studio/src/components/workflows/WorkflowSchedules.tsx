"use client";

import React from "react";
import { DataList, type DataListColumn } from "@/components/shared/DataList";

export interface WorkflowScheduleRow {
  id: string;
  target: string;
  cron: string;
}

const columns: DataListColumn<WorkflowScheduleRow>[] = [
  { id: "target", header: "Target", sortable: true, sortValue: (row) => row.target.toLowerCase(), cell: (row) => row.target },
  { id: "id", header: "Schedule ID", cell: (row) => <span className="font-mono text-label">{row.id}</span> },
  { id: "cron", header: "Cron", cell: (row) => <span className="font-mono text-label">{row.cron}</span> },
];

/**
 * The Schedules table from the workflow tour: Target, Schedule ID, Cron.
 * There is no schedule API yet, so the table is empty.
 */
export function WorkflowSchedules({ target }: { target?: string }) {
  return (
    <DataList<WorkflowScheduleRow>
      label="Schedules"
      rows={[]}
      columns={columns}
      getRowId={(row) => row.id}
      searchPlaceholder="Filter by id or workflow"
      filterRow={(row, query) => {
        const q = query.toLowerCase();
        return row.id.toLowerCase().includes(q) || row.target.toLowerCase().includes(q);
      }}
      loading={false}
      empty={{
        title: "No schedules",
        description: target
          ? `${target} has no cron schedule. A schedule would start a run on its own.`
          : "No workflow has a cron schedule.",
      }}
      testId="workflow-schedules"
    />
  );
}
