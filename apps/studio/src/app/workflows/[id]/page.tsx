"use client";

import React, { useEffect, useState } from "react";
import { useParams, useRouter } from "next/navigation";
import { ApiError } from "@/lib/api";
import { getWorkflow, listWorkflowVersions, type Workflow, type WorkflowVersion } from "@/lib/workflows";
import { WorkflowGraph } from "@/components/workflows/WorkflowGraph";
import { WorkflowRunPanel } from "@/components/workflows/WorkflowRunPanel";
import { WorkflowRunHistory } from "@/components/workflows/WorkflowRunHistory";
import { formatDateTime } from "@/components/workflows/ApprovalCard";
import { routes } from "@/lib/routes";
import { PageHeader } from "@/components/frame/PageHeader";
import { navCrumb } from "@/lib/nav";

export default function WorkflowDetailPage() {
  const params = useParams();
  const router = useRouter();
  const id = typeof params.id === "string" ? params.id : "";
  const [workflow, setWorkflow] = useState<Workflow | null>(null);
  const [versions, setVersions] = useState<WorkflowVersion[]>([]);
  const [selectedVersion, setSelectedVersion] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notFound, setNotFound] = useState(false);

  useEffect(() => {
    if (!id) return;
    let cancelled = false;
    Promise.all([getWorkflow(id), listWorkflowVersions(id)])
      .then(([wf, vs]) => {
        if (cancelled) return;
        setWorkflow(wf);
        setVersions(vs);
        setSelectedVersion(vs[0]?.version ?? null);
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

  const current = versions.find((v) => v.version === selectedVersion) ?? null;
  const spec = current?.spec ?? null;
  const graphSpec = spec ?? workflow?.draft_spec ?? null;

  return (
    <>
      <PageHeader
        crumbs={[navCrumb("workflows"), { label: workflow?.name ?? "Workflow" }]}
        actions={
          workflow ? (
            <div className="flex items-center gap-2">
              <label htmlFor="workflow-version" className="text-sm text-muted-foreground">Version</label>
              <select
                id="workflow-version"
                data-testid="workflow-version-select"
                value={selectedVersion ?? ""}
                disabled={versions.length === 0}
                onChange={(event) => setSelectedVersion(Number(event.target.value))}
                className="rounded-md border border-input bg-background px-3 py-1.5 text-sm text-foreground"
              >
                {versions.length === 0 ? <option value="">No published version</option> : null}
                {versions.map((v) => (
                  <option key={v.version} value={v.version}>v{v.version}{v.version === workflow.latest_version ? " (latest)" : ""}</option>
                ))}
              </select>
            </div>
          ) : null
        }
      />
    <div className="mx-auto max-w-7xl space-y-6 px-4 py-8">
      {notFound ? <p role="alert" data-testid="workflow-not-found" className="rounded-md border border-destructive/30 bg-destructive/10 p-3 text-sm text-destructive-text">Workflow not found.</p> : null}
      {error ? <p role="alert" className="rounded-md border border-destructive/30 bg-destructive/10 p-3 text-sm text-destructive-text">{error}</p> : null}
      {!workflow && !notFound && !error ? <p className="text-muted-foreground">Loading workflow…</p> : null}
      {workflow ? (
        <>
          {graphSpec?.description ? <p className="max-w-3xl text-muted-foreground">{graphSpec.description}</p> : null}
          {current ? (
            <p className="text-xs text-muted-foreground">Published {formatDateTime(current.created_at)} by <span className="font-mono">{current.created_by}</span></p>
          ) : (
            <p role="status" className="rounded-md border border-border bg-muted/40 p-3 text-sm text-foreground">No published version: showing the draft. Publish it (POST /api/v1/workflows/{"{id}"}/versions) to run it.</p>
          )}
          <section aria-labelledby="workflow-graph-heading" className="space-y-3">
            <h2 id="workflow-graph-heading" className="font-semibold text-foreground">Execution graph</h2>
            {graphSpec ? <WorkflowGraph key={`${id}:${selectedVersion ?? "draft"}`} spec={graphSpec} /> : <p className="text-sm text-muted-foreground">No definition to show.</p>}
          </section>
          <div className="grid gap-6 lg:grid-cols-2">
            <WorkflowRunPanel
              workflowId={workflow.id}
              workflowName={workflow.name}
              version={current?.version ?? null}
              spec={spec}
              onStarted={(runId) => router.push(routes.workflowRun(workflow.id, runId))}
            />
            <WorkflowRunHistory workflowId={workflow.id} />
          </div>
        </>
      ) : null}
    </div>
    </>
  );
}
