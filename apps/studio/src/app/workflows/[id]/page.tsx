"use client";

import React, { Suspense, useEffect, useState } from "react";
import Link from "next/link";
import { useParams, useRouter, useSearchParams } from "next/navigation";
import { ApiError } from "@/lib/api";
import { getWorkflow, listWorkflowVersions, type Workflow, type WorkflowVersion } from "@/lib/workflows";
import { WorkflowCanvas, WorkflowStepList } from "@/components/workflows/WorkflowGraph";
import { WorkflowRunPanel } from "@/components/workflows/WorkflowRunPanel";
import { WorkflowRunHistory } from "@/components/workflows/WorkflowRunHistory";
import { formatDateTime } from "@/components/approvals/ApprovalCard";
import { PanelCard, useWorkflowLayout, WorkflowFloatingPanel } from "@/components/workflows/WorkflowFloatingPanel";
import { WorkflowHeader } from "@/components/workflows/WorkflowHeader";
import { useCollapsedLoops } from "@/components/workflows/useCollapsedLoops";
import { TraceSheetHost } from "@/components/traces/TraceSheetHost";
import { routes } from "@/lib/routes";
import { parseWorkflowTab, WORKFLOW_TAB_PARAM, WORKFLOW_TRACE_TAB_PARAM } from "@/lib/routes/workflows";

/** How many runs the Graph tab's Recent runs card lists. */
const RECENT_RUNS = 5;

const selectClass =
  "h-7 rounded-md border border-input bg-surface px-2 text-label text-foreground outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:opacity-50";

/**
 * `/workflows/:id` (SL.14): tabs Graph (default) and Runs (`?tab=runs`); Traces is its own path.
 * Graph is a full-bleed canvas with the floating panel: Run, Recent runs and the step list.
 */
function WorkflowDetail() {
  const params = useParams();
  const router = useRouter();
  const searchParams = useSearchParams();
  const tab = parseWorkflowTab(searchParams?.get(WORKFLOW_TAB_PARAM));
  const id = typeof params.id === "string" ? params.id : "";
  const [workflow, setWorkflow] = useState<Workflow | null>(null);
  const [versions, setVersions] = useState<WorkflowVersion[]>([]);
  const [selectedVersion, setSelectedVersion] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notFound, setNotFound] = useState(false);
  const control = useWorkflowLayout();
  // The graph's collapsed loops are URL state (`?collapsed=`), kept across a version switch and
  // carried to the run view.
  const { collapsed, setCollapsed } = useCollapsedLoops();

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

  const versionSelect = workflow ? (
    <div className="flex items-center gap-2">
      <label htmlFor="workflow-version" className="text-label text-muted-foreground">Version</label>
      <select
        id="workflow-version"
        data-testid="workflow-version-select"
        value={selectedVersion ?? ""}
        disabled={versions.length === 0}
        onChange={(event) => setSelectedVersion(Number(event.target.value))}
        className={selectClass}
      >
        {versions.length === 0 ? <option value="">No published version</option> : null}
        {versions.map((v) => (
          <option key={v.version} value={v.version}>v{v.version}{v.version === workflow.latest_version ? " (latest)" : ""}</option>
        ))}
      </select>
    </div>
  ) : null;

  return (
    <TraceSheetHost tabParam={WORKFLOW_TRACE_TAB_PARAM}>
      <div className="flex h-full min-h-[32rem] min-w-0 flex-col">
        <WorkflowHeader workflowId={id} name={workflow?.name ?? null} tab={tab} actions={versionSelect} />
        {notFound || error || !workflow ? (
          <div className="p-4">
            {notFound ? <p role="alert" data-testid="workflow-not-found" className="rounded-md border border-destructive/30 bg-destructive/10 p-3 text-body text-destructive-text">Workflow not found.</p> : null}
            {error ? <p role="alert" className="rounded-md border border-destructive/30 bg-destructive/10 p-3 text-body text-destructive-text">{error}</p> : null}
            {!workflow && !notFound && !error ? <p className="text-muted-foreground">Loading workflow…</p> : null}
          </div>
        ) : null}

        {workflow && tab === "graph" ? (
          <div className="relative min-h-0 min-w-0 flex-1">
            <WorkflowFloatingPanel control={control} title="Workflow">
              <div className="min-w-0 space-y-2 border-b border-border p-4 empty:hidden">
                {graphSpec?.description ? <p className="text-label text-muted-foreground">{graphSpec.description}</p> : null}
                {current ? (
                  <p className="text-caption text-muted-foreground">Published {formatDateTime(current.created_at)} by <span className="font-mono">{current.created_by}</span></p>
                ) : (
                  <p role="status" className="rounded-md border border-border bg-muted/40 p-2 text-label text-foreground">No published version: showing the draft. Publish it (POST /api/v1/workflows/{"{id}"}/versions) to run it.</p>
                )}
              </div>
              <WorkflowRunPanel
                workflowId={workflow.id}
                workflowName={workflow.name}
                version={current?.version ?? null}
                spec={spec}
                onStarted={(runId) => router.push(routes.workflowRun(workflow.id, runId, { collapsed }))}
              />
              <WorkflowRunHistory
                workflowId={workflow.id}
                limit={RECENT_RUNS}
                title="Recent runs"
                variant="panel"
                footer={
                  <Link href={routes.workflow(workflow.id, { tab: "runs" })} data-testid="workflow-all-runs" className="rounded-sm text-label text-info-text underline-offset-4 outline-none hover:underline focus-visible:ring-2 focus-visible:ring-ring">
                    All runs
                  </Link>
                }
              />
              {graphSpec ? (
                <PanelCard title="Steps" titleId="workflow-steps-heading">
                  <WorkflowStepList key={`${id}:${selectedVersion ?? "draft"}`} spec={graphSpec} collapsedLoops={collapsed} onCollapsedLoopsChange={setCollapsed} />
                </PanelCard>
              ) : null}
            </WorkflowFloatingPanel>
            {graphSpec ? (
              <WorkflowCanvas key={`${id}:${selectedVersion ?? "draft"}`} spec={graphSpec} className="absolute inset-0" collapsedLoops={collapsed} onCollapsedLoopsChange={setCollapsed} />
            ) : (
              <div className="absolute inset-0 flex items-center justify-center bg-background">
                <p className="text-label text-muted-foreground">No definition to show.</p>
              </div>
            )}
          </div>
        ) : null}

        {workflow && tab === "runs" ? (
          <div className="min-w-0 p-4">
            <WorkflowRunHistory workflowId={workflow.id} onRerunStarted={(newRunId) => router.push(routes.workflowRun(workflow.id, newRunId))} />
          </div>
        ) : null}
      </div>
    </TraceSheetHost>
  );
}

export default function WorkflowDetailPage() {
  // useSearchParams (the tab, the trace Sheet) needs a Suspense boundary for the static prerender.
  return (
    <Suspense fallback={<p className="p-4 text-muted-foreground">Loading workflow…</p>}>
      <WorkflowDetail />
    </Suspense>
  );
}
