"use client";

import React, { Suspense, useCallback, useEffect, useMemo, useState } from "react";
import Link from "next/link";
import { useParams, useRouter, useSearchParams } from "next/navigation";
import { api, ApiError, type Agent } from "@/lib/api";
import { listTools, type ToolDefinition } from "@/lib/tools";
import { StepDefinitionPanel, type StepAgentInfo } from "@/components/workflows/StepDefinitionPanel";
import { buildWorkflowGraph } from "@/lib/workflowGraph";
import { getWorkflow, listWorkflowVersions, type Workflow, type WorkflowStep, type WorkflowVersion } from "@/lib/workflows";
import { WorkflowCanvas, WorkflowStepList } from "@/components/workflows/WorkflowGraph";
import { WorkflowRunPanel } from "@/components/workflows/WorkflowRunPanel";
import { WorkflowRunHistory } from "@/components/workflows/WorkflowRunHistory";
import { formatDateTime } from "@/components/approvals/ApprovalCard";
import { PanelCard, useWorkflowLayout, WorkflowFloatingPanel } from "@/components/workflows/WorkflowFloatingPanel";
import { WorkflowArchivedBanner, WorkflowArchiveMenu } from "@/components/workflows/WorkflowArchiveMenu";
import { WorkflowHeader } from "@/components/workflows/WorkflowHeader";
import { useCollapsedLoops } from "@/components/workflows/useCollapsedLoops";
import { TraceSheetHost } from "@/components/traces/TraceSheetHost";
import { routes } from "@/lib/routes";
import { parseWorkflowTab, WORKFLOW_TAB_PARAM, WORKFLOW_TRACE_TAB_PARAM } from "@/lib/routes/workflows";

/** How many runs the Graph tab's Recent runs card lists. */
const RECENT_RUNS = 5;

const selectClass =
  "h-7 rounded-md border border-input bg-surface px-2 text-label text-foreground outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:opacity-50";

/** The step with this id, anywhere in the spec (a loop's body included). */
function findStepById(spec: { steps: WorkflowStep[] } | null, id: string | null): WorkflowStep | null {
  if (!spec || !id) return null;
  const node = buildWorkflowGraph(spec as Parameters<typeof buildWorkflowGraph>[0]).nodes.find((n) => n.id === id);
  return node?.step ?? null;
}

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

  const [tools, setTools] = useState<ToolDefinition[]>([]);
  const [agents, setAgents] = useState<Agent[]>([]);
  const [selectedStepId, setSelectedStepId] = useState<string | null>(null);
  const clearStep = useCallback(() => setSelectedStepId(null), []);
  // Tool declarations and agent names say what a step does; each is best effort (a step still shows its ids without them).
  useEffect(() => {
    let cancelled = false;
    listTools().then((t) => { if (!cancelled) setTools(t); }).catch(() => undefined);
    api.agents.list().then((a) => { if (!cancelled) setAgents(a); }).catch(() => undefined);
    return () => { cancelled = true; };
  }, []);
  const agentNames = useMemo(() => Object.fromEntries(agents.map((a) => [a.id, a.name])), [agents]);

  const current = versions.find((v) => v.version === selectedVersion) ?? null;
  const spec = current?.spec ?? null;
  const graphSpec = spec ?? workflow?.draft_spec ?? null;

  const selectedStep = findStepById(graphSpec, selectedStepId);
  const stepAgent: StepAgentInfo | null = (() => {
    if (selectedStep?.type !== "agent") return null;
    const found = agents.find((a) => a.id === selectedStep.agent_id);
    const model = found?.latest_spec_summary?.model;
    return {
      id: selectedStep.agent_id,
      name: found?.name ?? selectedStep.agent_id,
      version: selectedStep.version ?? found?.latest_version ?? null,
      pinned: Boolean(selectedStep.version),
      model: model?.name ? [model.provider, model.name].filter(Boolean).join("/") : null,
    };
  })();
  const stepTool = selectedStep?.type === "tool" ? tools.find((t) => t.id === selectedStep.tool_id) ?? null : null;
  const stepNext = selectedStep && selectedStep.type !== "branch" && graphSpec
    ? buildWorkflowGraph(graphSpec).edges.filter((e) => e.source === selectedStep.id).map((e) => e.target)
    : [];
  const toggleStep = (stepId: string) => setSelectedStepId((prev) => (prev === stepId ? null : stepId));

  const versionSelect = workflow ? (
    <div className="flex items-center gap-2">
      <label htmlFor="workflow-version" className="text-label text-muted-foreground">Version</label>
      <select
        id="workflow-version"
        data-testid="workflow-version-select"
        value={selectedVersion ?? ""}
        disabled={versions.length === 0}
        onChange={(event) => { setSelectedVersion(Number(event.target.value)); setSelectedStepId(null); }}
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
        <WorkflowHeader workflowId={id} name={workflow?.name ?? null} tab={tab} actions={
          <>
            {versionSelect}
            {workflow ? <WorkflowArchiveMenu workflow={workflow} onChanged={setWorkflow} /> : null}
          </>
        } />
        {workflow?.archived ? <WorkflowArchivedBanner workflow={workflow} onChanged={setWorkflow} /> : null}
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
              {graphSpec && selectedStep ? (
                <PanelCard title="Step details" titleId="workflow-step-details-heading" testId="workflow-step-details">
                  <StepDefinitionPanel
                    step={selectedStep}
                    tool={stepTool}
                    agent={stepAgent}
                    next={stepNext}
                    // Hook: the step's cached AI 註解 goes here once lib/workflowExplanations.ts is on main.
                    explanation={null}
                    onClose={clearStep}
                  />
                </PanelCard>
              ) : null}
              {graphSpec ? (
                <PanelCard title="Steps" titleId="workflow-steps-heading">
                  <WorkflowStepList key={`${id}:${selectedVersion ?? "draft"}`} spec={graphSpec} collapsedLoops={collapsed} onCollapsedLoopsChange={setCollapsed} selectedStepId={selectedStepId} onSelectStep={toggleStep} agentNames={agentNames} />
                </PanelCard>
              ) : null}
            </WorkflowFloatingPanel>
            {graphSpec ? (
              <WorkflowCanvas key={`${id}:${selectedVersion ?? "draft"}`} spec={graphSpec} className="absolute inset-0" collapsedLoops={collapsed} onCollapsedLoopsChange={setCollapsed} selectedStepId={selectedStepId} onSelectStep={toggleStep} agentNames={agentNames} />
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
