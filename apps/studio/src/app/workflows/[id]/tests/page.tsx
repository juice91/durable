"use client";

import React, { Suspense, useEffect, useState } from "react";
import { useParams } from "next/navigation";
import { TestsTab } from "@/components/workflowPage/TestsTab";
import { WorkflowHeader } from "@/components/workflows/WorkflowHeader";
import { ApiError } from "@/lib/api";
import { getWorkflow, type Workflow } from "@/lib/workflows";

function WorkflowTestsPage() {
  const params = useParams();
  const id = typeof params.id === "string" ? params.id : "";
  const [workflow, setWorkflow] = useState<Workflow | null>(null);
  const [notFound, setNotFound] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!id) return;
    let cancelled = false;
    getWorkflow(id)
      .then((wf) => {
        if (!cancelled) setWorkflow(wf);
      })
      .catch((reason: unknown) => {
        if (cancelled) return;
        if (reason instanceof ApiError && reason.status === 404) setNotFound(true);
        else setError(reason instanceof Error ? reason.message : "Failed to load the workflow");
      });
    return () => {
      cancelled = true;
    };
  }, [id]);

  return (
    <div className="flex h-full min-h-[32rem] min-w-0 flex-col">
      <WorkflowHeader workflowId={id} name={workflow?.name ?? null} tab="tests" />
      {notFound ? <p role="alert" className="m-4 rounded-md border border-destructive/30 bg-destructive/10 p-3 text-body text-destructive-text">Workflow not found.</p> : null}
      {error ? <p role="alert" className="m-4 rounded-md border border-destructive/30 bg-destructive/10 p-3 text-body text-destructive-text">{error}</p> : null}
      {workflow ? <TestsTab workflowId={id} name={workflow.name} /> : null}
      {!workflow && !notFound && !error ? <p className="p-4 text-muted-foreground">Loading workflow…</p> : null}
    </div>
  );
}

export default function WorkflowTestsRoute() {
  // The tab reads its URL state (useSearchParams), which needs a Suspense boundary.
  return (
    <Suspense fallback={<p className="p-4 text-muted-foreground">Loading tests…</p>}>
      <WorkflowTestsPage />
    </Suspense>
  );
}
