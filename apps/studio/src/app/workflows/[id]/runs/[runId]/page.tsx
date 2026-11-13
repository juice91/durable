"use client";

import React from "react";
import Link from "next/link";
import { useParams } from "next/navigation";
import { WorkflowRunView } from "@/components/workflows/WorkflowRunView";
import { routes } from "@/lib/routes";

export default function WorkflowRunPage() {
  const params = useParams();
  const id = typeof params.id === "string" ? params.id : "";
  const runId = typeof params.runId === "string" ? params.runId : "";
  return (
    <div className="mx-auto max-w-7xl space-y-4 px-4 py-8">
      <nav aria-label="Breadcrumb" className="flex gap-2 text-sm">
        <Link href={routes.workflows()} className="text-primary underline-offset-4 hover:underline">Workflows</Link>
        <span aria-hidden="true" className="text-muted-foreground">/</span>
        <Link href={routes.workflow(id)} className="text-primary underline-offset-4 hover:underline">Workflow</Link>
      </nav>
      {runId ? <WorkflowRunView key={runId} runId={runId} workflowId={id} /> : null}
    </div>
  );
}
