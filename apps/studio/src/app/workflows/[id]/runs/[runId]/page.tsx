"use client";

import React, { Suspense } from "react";
import { useParams } from "next/navigation";
import { WorkflowRunView } from "@/components/workflows/WorkflowRunView";
import { useCollapsedLoops } from "@/components/workflows/useCollapsedLoops";
import { workflowTabs } from "@/components/workflows/WorkflowHeader";
import { TraceSheetHost } from "@/components/traces/TraceSheetHost";
import { WORKFLOW_TRACE_TAB_PARAM } from "@/lib/routes/workflows";

/**
 * `/workflows/:id/runs/:runId` (URL kept, SL.14): the run view under the workflow's Graph tab. It
 * hosts the trace Sheet ("Run details", child runs) on `traceTab`, as the other workflow pages do.
 */
function WorkflowRun() {
  const params = useParams();
  const id = typeof params.id === "string" ? params.id : "";
  const runId = typeof params.runId === "string" ? params.runId : "";
  const { collapsed, setCollapsed } = useCollapsedLoops();
  return (
    <TraceSheetHost tabParam={WORKFLOW_TRACE_TAB_PARAM}>
      {runId ? <WorkflowRunView
          key={runId}
          runId={runId}
          workflowId={id}
          tabs={id ? workflowTabs(id, "graph") : undefined}
          collapsedLoops={collapsed}
          onCollapsedLoopsChange={setCollapsed}
        /> : null}
    </TraceSheetHost>
  );
}

export default function WorkflowRunPage() {
  // useSearchParams (the trace Sheet) needs a Suspense boundary for the static prerender.
  return (
    <Suspense fallback={<p className="p-4 text-muted-foreground">Loading run…</p>}>
      <WorkflowRun />
    </Suspense>
  );
}
