"use client";

import React, { Suspense, useEffect, useState } from "react";
import { useParams } from "next/navigation";
import { WorkflowHeader } from "@/components/workflows/WorkflowHeader";
import { WorkflowSchedules } from "@/components/workflows/WorkflowSchedules";
import { ApiError } from "@/lib/api";
import { getWorkflow, getWorkflowVersion, type Workflow, type WorkflowSpec } from "@/lib/workflows";

function WorkflowSchedulesTab() {
  const params = useParams();
  const id = typeof params.id === "string" ? params.id : "";
  const [workflow, setWorkflow] = useState<Workflow | null>(null);
  const [notFound, setNotFound] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [spec, setSpec] = useState<WorkflowSpec | null>(null);
  const [count, setCount] = useState<number | undefined>(undefined);

  useEffect(() => {
    if (!id) return;
    let cancelled = false;
    getWorkflow(id)
      .then((wf) => {
        if (cancelled) return;
        setWorkflow(wf);
        setSpec(wf.draft_spec);
        // Schedules run the latest published version: its input schema builds the New schedule form.
        if (wf.latest_version != null) {
          getWorkflowVersion(id, wf.latest_version)
            .then((version) => {
              if (!cancelled) setSpec(version.spec);
            })
            .catch(() => undefined);
        }
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
      <WorkflowHeader workflowId={id} name={workflow?.name ?? null} tab="schedules" scheduleCount={count} />
      <div className="min-w-0 p-4">
        {notFound ? <p role="alert" className="rounded-md border border-destructive/30 bg-destructive/10 p-3 text-body text-destructive-text">Workflow not found.</p> : null}
        {error ? <p role="alert" className="rounded-md border border-destructive/30 bg-destructive/10 p-3 text-body text-destructive-text">{error}</p> : null}
        {workflow ? <WorkflowSchedules workflowId={id} target={workflow.name} spec={spec} onCountChange={setCount} /> : null}
        {!workflow && !notFound && !error ? <p className="text-muted-foreground">Loading workflow…</p> : null}
      </div>
    </div>
  );
}

export default function WorkflowSchedulesTabPage() {
  return (
    <Suspense fallback={<p className="p-4 text-muted-foreground">Loading schedules…</p>}>
      <WorkflowSchedulesTab />
    </Suspense>
  );
}
