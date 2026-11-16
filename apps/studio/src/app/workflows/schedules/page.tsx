"use client";

import React from "react";
import { PageHeader } from "@/components/frame/PageHeader";
import { WorkflowSchedules } from "@/components/workflows/WorkflowSchedules";
import { navCrumb } from "@/lib/nav";

/** `/workflows/schedules`: the Schedules button on the workflow list. */
export default function WorkflowSchedulesPage() {
  return (
    <>
      <PageHeader crumbs={[navCrumb("workflows"), { label: "Schedules" }]} documentTitle="Schedules" />
      <div className="flex min-w-0 flex-col gap-4 p-4">
        <WorkflowSchedules />
      </div>
    </>
  );
}
