"use client";

import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import Link from "next/link";
import { StatusPill } from "@/components/runs/StatusPill";
import { EventTimeline, formatValue } from "@/components/runs/EventTimeline";
import { ApiError } from "@/lib/api";
import { getRun, listEvents, subscribeRun, type Run, type RunEvent, type RunStatus } from "@/lib/runs";
import { deriveRunView, isTerminal, mergeEvents, type NodeState } from "@/lib/workflowRunState";
import { getWorkflowVersion, type WorkflowSpec } from "@/lib/workflows";
import { Copy } from "lucide-react";
import { ICON_STROKE_WIDTH } from "@/lib/icons";
import { navCrumb } from "@/lib/nav";
import { routes } from "@/lib/routes";
import { Badge } from "@/components/ui/Badge";
import { IconButton } from "@/components/ui/IconButton";
import { Sheet, SheetBody, SheetContent, SheetHeader } from "@/components/ui/Sheet";
import { toast } from "@/components/ui/Toaster";
import type { LinkTab } from "@/components/ui/Tabs";
import { PageHeader } from "@/components/frame/PageHeader";
import { TraceLink } from "@/components/traces/TraceLink";
import { WorkflowCanvas, WorkflowStepList } from "./WorkflowGraph";
import { StepIoPanel } from "./StepIoPanel";
import { ApprovalCard, formatDateTime } from "@/components/approvals/ApprovalCard";
import { formatRunDuration } from "./WorkflowRunHistory";
import { PanelCard, useWorkflowLayout, WorkflowFloatingPanel } from "./WorkflowFloatingPanel";
import { TimelineDock } from "./TimelineDock";
import { CANVAS_INSET_PX, STEP_PANEL_WIDTH } from "./workflowLayout";
import { timelineClock, timelineRows } from "./workflowTimeline";
import { copyText } from "@/lib/clipboard";

/** How often a live run's open timeline bars grow. */
const TIMELINE_TICK_MS = 1000;

/** The approvals overlay's widest, when no step detail shares the right column. */
const APPROVAL_OVERLAY_MAX_WIDTH = 416;

type StreamState = "connecting" | "live" | "reconnecting" | "ended";

const STREAM_LABELS: Record<StreamState, string> = {
  connecting: "Connecting…",
  live: "Live",
  reconnecting: "Reconnecting…",
  ended: "Stream ended",
};

interface WorkflowRunViewProps {
  runId: string;
  /** The workflow id from the URL; a mismatch with the run's own workflow is reported. */
  workflowId?: string;
  /** The workflow page's tabs, shown in the header (the run view belongs to Graph). */
  tabs?: LinkTab[];
  /** The graph's collapsed loops (the page's `?collapsed=` state), shared by the canvas and the step list. */
  collapsedLoops?: ReadonlySet<string>;
  onCollapsedLoopsChange?: (next: Set<string>) => void;
}

/**
 * The live execution view of one workflow run (SL.14): the version's graph, full-bleed, with node
 * states from the run stream; a floating panel with the run's status, duration, id and the step
 * list; waiting approvals as a "Needs input" overlay; the selected step's I/O in a right column (a
 * Sheet where it does not fit); and the bottom Timeline dock. Events come from GET /events
 * (backfill and refresh) and the SSE stream; both are merged by seq, so duplicates and replays are
 * harmless.
 */
export function WorkflowRunView({ runId, workflowId, tabs, collapsedLoops, onCollapsedLoopsChange }: WorkflowRunViewProps) {
  // One collapsed set for the canvas and the step list: the page's URL state, else kept here.
  const [ownCollapsed, setOwnCollapsed] = useState<ReadonlySet<string>>(() => new Set());
  const collapsed = collapsedLoops ?? ownCollapsed;
  const setCollapsed = onCollapsedLoopsChange ?? setOwnCollapsed;
  const [run, setRun] = useState<Run | null>(null);
  const [spec, setSpec] = useState<WorkflowSpec | null>(null);
  const [events, setEvents] = useState<RunEvent[]>([]);
  const [status, setStatus] = useState<RunStatus>("queued");
  const [loadError, setLoadError] = useState<string | null>(null);
  const [specError, setSpecError] = useState<string | null>(null);
  const [notFound, setNotFound] = useState(false);
  const [stream, setStream] = useState<StreamState>("connecting");
  const [selected, setSelected] = useState<string | null>(null);
  const alive = useRef(true);

  const addEvents = useCallback((incoming: RunEvent[]) => {
    if (!alive.current || incoming.length === 0) return;
    setEvents((prev) => mergeEvents(prev, incoming));
  }, []);

  /** Re-read the run row and every event (after the stream ends, a decision, or a 409). */
  const refresh = useCallback(async () => {
    try {
      const [row, all] = await Promise.all([getRun(runId), listEvents(runId)]);
      if (!alive.current) return;
      setRun(row);
      setStatus(row.display_status);
      addEvents(all);
    } catch (reason) {
      if (!alive.current) return;
      if (reason instanceof ApiError && reason.status === 404) setNotFound(true);
      else setLoadError(reason instanceof Error ? reason.message : "Failed to refresh the run");
    }
  }, [runId, addEvents]);

  useEffect(() => {
    alive.current = true;
    setRun(null);
    setSpec(null);
    setEvents([]);
    setStatus("queued");
    setLoadError(null);
    setSpecError(null);
    setNotFound(false);
    setStream("connecting");
    setSelected(null);

    getRun(runId)
      .then(async (row) => {
        if (!alive.current) return;
        setRun(row);
        setStatus((current) => (isTerminal(current) ? current : row.display_status));
        if (row.kind !== "workflow" || !row.workflow_id || row.workflow_version == null) {
          setLoadError("This run is not a workflow run.");
          return;
        }
        try {
          const version = await getWorkflowVersion(row.workflow_id, row.workflow_version);
          if (alive.current) setSpec(version.spec);
        } catch (reason) {
          if (alive.current) setSpecError(reason instanceof Error ? reason.message : "Failed to load the workflow version");
        }
      })
      .catch((reason: unknown) => {
        if (!alive.current) return;
        if (reason instanceof ApiError && reason.status === 404) setNotFound(true);
        else setLoadError(reason instanceof Error ? reason.message : "Failed to load the run");
      });

    listEvents(runId).then(addEvents).catch(() => {
      // The stream replays from the start as well; a failed backfill is not fatal.
    });

    let unsubscribe: () => void = () => undefined;
    try {
      unsubscribe = subscribeRun(runId, {
        onEvent: (event) => {
          if (!alive.current) return;
          setStream("live");
          addEvents([event]);
        },
        onStatus: (frame) => {
          if (!alive.current || !frame.display_status) return;
          setStream("live");
          setStatus(frame.display_status);
        },
        onError: () => {
          if (alive.current) setStream("reconnecting");
        },
        onEnd: (reason) => {
          if (!alive.current) return;
          setStream("ended");
          if (reason === "not_found") setNotFound(true);
          else void refresh();
        },
      });
    } catch (reason) {
      setLoadError(reason instanceof Error ? reason.message : "Failed to follow the run");
    }
    return () => {
      alive.current = false;
      unsubscribe();
    };
  }, [runId, addEvents, refresh]);

  const view = useMemo(() => deriveRunView(spec, events, status), [spec, events, status]);
  const states = useMemo(() => {
    const out: Record<string, NodeState> = {};
    for (const [id, d] of Object.entries(view.steps)) out[id] = d.state;
    return out;
  }, [view]);
  // The live clock that grows open bars: ticks once a second only while the run is live and a step
  // is open; an ended run's bars close at the run's end instead (`timelineClock`).
  const [now, setNow] = useState(() => Date.now());
  const live = !isTerminal(view.status) && Object.values(view.steps).some((d) => d.state === "running" || d.state === "waiting");
  useEffect(() => {
    if (!live) return;
    setNow(Date.now());
    const timer = setInterval(() => setNow(Date.now()), TIMELINE_TICK_MS);
    return () => clearInterval(timer);
  }, [live]);
  const clock = timelineClock(view, run?.updated_at, now);
  const rows = useMemo(() => timelineRows(spec, view, clock), [spec, view, clock]);
  const stepEventCount = events.filter((e) => e.type.startsWith("step_")).length;
  const selectedDetail = selected ? view.steps[selected] ?? null : null;
  const selectedStep = selected && spec ? spec.steps.find((s) => s.id === selected) ?? null : null;
  const effectiveWorkflowId = run?.workflow_id ?? workflowId ?? null;
  const control = useWorkflowLayout();
  const { layout, prefs, update } = control;
  const toggleStep = useCallback((id: string) => setSelected((cur) => (cur === id ? null : id)), []);

  // After Approve or Reject the decided card (often the whole overlay) goes away with the focus in
  // it: send focus to the run status rather than leave it on <body>.
  const statusRef = useRef<HTMLSpanElement>(null);
  const refocusAfterDecision = useRef(false);
  const waitingKey = view.waiting.join("\n");
  useEffect(() => {
    if (!refocusAfterDecision.current) return;
    const active = typeof document !== "undefined" ? document.activeElement : null;
    if (active && active !== document.body && active.isConnected) return;
    refocusAfterDecision.current = false;
    statusRef.current?.focus();
  }, [waitingKey]);

  const runLabel = `Run ${runId.slice(0, 8)}`;
  const header = (
    <PageHeader
      crumbs={[
        navCrumb("workflows"),
        { label: spec?.name ?? "Workflow", href: effectiveWorkflowId ? routes.workflow(effectiveWorkflowId, { collapsed }) : undefined },
        { label: <span className="font-mono">{runLabel}</span> },
      ]}
      documentTitle={`${runLabel} · ${spec?.name ?? "Workflow"}`}
      tabs={tabs}
      tabsLabel="Workflow sections"
      actions={
        <>
          <span data-testid="workflow-run-stream" data-state={stream} aria-live="polite" className="text-caption text-muted-foreground">{STREAM_LABELS[stream]}</span>
          <span ref={statusRef} tabIndex={-1} data-testid="workflow-run-status" data-status={view.status} className="rounded-sm outline-none focus-visible:ring-2 focus-visible:ring-ring"><StatusPill status={view.status} showRecoveringNotice /></span>
        </>
      }
    />
  );

  if (notFound) {
    return (
      <div className="flex min-w-0 flex-col">
        {header}
        <div className="p-4">
          <div role="alert" data-testid="workflow-run-not-found" className="rounded-md border border-destructive/30 bg-destructive/10 p-4 text-body text-destructive-text">
            Run not found.
          </div>
        </div>
      </div>
    );
  }

  const duration = isTerminal(view.status) && run ? formatRunDuration(run.created_at, run.updated_at) : run ? "in progress" : "—";
  const copyId = () => {
    void copyText(runId).then((copied) => toast(copied ? "Run id copied" : "Could not copy the run id"));
  };

  const approvals = view.waiting.length > 0 ? (
    <section aria-label="Waiting approvals" data-testid="workflow-approval-overlay" className="pointer-events-auto min-w-0 space-y-2 rounded-lg border border-warning bg-surface-raised p-2 shadow-overlay">
      <p className="flex items-center gap-2 px-1">
        <Badge variant="warning" data-testid="workflow-needs-input">Needs input</Badge>
        <span className="text-caption text-muted-foreground">
          {view.waiting.length === 1 ? "A step is waiting for approval." : `${view.waiting.length} steps are waiting for approval.`}
        </span>
      </p>
      {view.waiting.map((id) => (
        <ApprovalCard
          key={id}
          approval={{ kind: "step", runId, stepId: id, prompt: view.steps[id]?.prompt ?? null, deadline: view.steps[id]?.deadline ?? null }}
          onRefresh={refresh}
          onDecided={() => {
            refocusAfterDecision.current = true;
          }}
        />
      ))}
    </section>
  ) : null;
  const floatingDetail = selectedDetail && layout.stepDetail === "floating" ? (
    <StepIoPanel step={selectedStep} detail={selectedDetail} onClose={() => setSelected(null)} className="pointer-events-auto min-w-0 space-y-3 rounded-lg border border-border bg-surface-raised p-4 shadow-overlay" />
  ) : null;
  const rightLeft = layout.leftOccupied > 0 ? layout.leftOccupied + 2 * CANVAS_INSET_PX : CANVAS_INSET_PX;

  return (
    <div data-testid="workflow-run-view" data-status={view.status} className="flex h-full min-h-[32rem] min-w-0 flex-col">
      {header}
      <div className="relative flex min-h-0 min-w-0 flex-1 flex-col">
        <div className="relative min-h-0 min-w-0 flex-1">
          <WorkflowFloatingPanel control={control} title="Run details">
            <PanelCard title="Run" titleId="workflow-run-summary-heading" testId="workflow-run-summary">
              <dl className="grid grid-cols-[auto_minmax(0,1fr)] gap-x-3 gap-y-1.5 text-label">
                <dt className="text-muted-foreground">Status</dt>
                <dd className="min-w-0"><StatusPill status={view.status} /></dd>
                <dt className="text-muted-foreground">Run id</dt>
                <dd className="flex min-w-0 items-center gap-1">
                  <span data-testid="workflow-run-id" className="truncate font-mono text-caption">{runId}</span>
                  <IconButton label="Copy run id" size="sm" tooltip={false} icon={<Copy strokeWidth={ICON_STROKE_WIDTH} aria-hidden="true" />} onClick={copyId} />
                </dd>
                {run?.workflow_version != null ? (
                  <>
                    <dt className="text-muted-foreground">Version</dt>
                    <dd>v{run.workflow_version}</dd>
                  </>
                ) : null}
                <dt className="text-muted-foreground">Started</dt>
                <dd className="min-w-0">{formatDateTime(run?.created_at)}</dd>
                <dt className="text-muted-foreground">Duration</dt>
                <dd data-testid="workflow-run-duration" className="tabular-nums">{duration}</dd>
              </dl>
              <p className="mt-3 flex flex-wrap gap-x-3 gap-y-1 text-label">
                {effectiveWorkflowId ? (
                  <Link href={routes.workflow(effectiveWorkflowId, { collapsed })} className="rounded-sm text-info-text underline-offset-4 outline-none hover:underline focus-visible:ring-2 focus-visible:ring-ring">Back to workflow</Link>
                ) : null}
                {/* The run's events in the trace Sheet, in place (SL.12). */}
                <TraceLink runId={runId} tab="events" className="rounded-sm text-info-text underline-offset-4 outline-none hover:underline focus-visible:ring-2 focus-visible:ring-ring">Run details</TraceLink>
              </p>

              <div className="mt-3 space-y-2 empty:hidden">
                {workflowId && run?.workflow_id && run.workflow_id !== workflowId ? (
                  <p role="status" className="rounded-md border border-border bg-muted/40 p-2 text-label text-foreground">This run belongs to another workflow; showing that workflow&apos;s graph.</p>
                ) : null}
                {loadError ? <p role="alert" className="rounded-md border border-destructive/30 bg-destructive/10 p-2 text-label text-destructive-text">{loadError}</p> : null}
                {specError ? <p role="alert" className="rounded-md border border-destructive/30 bg-destructive/10 p-2 text-label text-destructive-text">Graph unavailable: {specError}</p> : null}
                {view.status === "needs_attention" ? (
                  <div role="alert" data-testid="workflow-run-needs-attention" className="rounded-md border-2 border-warning bg-warning/10 p-2 text-label text-foreground">
                    <p className="font-semibold">Needs attention: a step may have had side effects whose outcome is unknown.</p>
                    <p className="mt-1 text-caption">It was not retried automatically. Check the target system before running the workflow again.</p>
                    {view.runError?.error ? <p className="mt-1 whitespace-pre-wrap text-caption">{view.runError.error}</p> : null}
                  </div>
                ) : null}
                {view.status === "failed" ? (
                  <div role="alert" data-testid="workflow-run-failed" className="rounded-md border border-destructive/30 bg-destructive/10 p-2 text-label text-destructive-text">
                    <p className="font-semibold">Failed{view.runError?.stepId ? ` at step ${view.runError.stepId}` : ""}</p>
                    {view.runError?.error ?? run?.error ? <p className="mt-1 whitespace-pre-wrap">{view.runError?.error ?? run?.error}</p> : null}
                    {view.runError?.errorCode ? <p className="mt-1 text-caption">Error code: <code data-testid="workflow-run-error-code" className="font-mono">{view.runError.errorCode}</code></p> : null}
                  </div>
                ) : null}
                {isTerminal(view.status) && stepEventCount === 0 ? (
                  <p role="status" data-testid="workflow-run-no-steps" className="rounded-md border border-border bg-muted/40 p-2 text-label text-foreground">
                    The run ended ({view.status.replace("_", " ")}) before any step ran.
                  </p>
                ) : null}
                {view.status === "completed" && view.output !== undefined ? (
                  <details className="rounded-md border border-border bg-surface p-2" open>
                    <summary className="cursor-pointer text-label font-semibold text-foreground">Output</summary>
                    <pre data-testid="workflow-run-output" className="mt-2 max-h-64 overflow-auto whitespace-pre-wrap break-words rounded-md bg-muted p-2 font-mono text-caption text-foreground">{formatValue(view.output)}</pre>
                  </details>
                ) : null}
              </div>
            </PanelCard>

            {spec ? (
              <PanelCard title="Steps" titleId="workflow-run-steps-heading">
                <p className="mb-2 text-caption text-muted-foreground">Select a step to see its input and output.</p>
                <WorkflowStepList spec={spec} states={states} selectedStepId={selected} onSelectStep={toggleStep} legend collapsedLoops={collapsed} onCollapsedLoopsChange={setCollapsed} />
              </PanelCard>
            ) : null}

            <PanelCard title={`Events (${events.length})`} titleId="workflow-run-events-heading">
              <details className="min-w-0">
                <summary className="cursor-pointer text-label text-foreground">Show the event list</summary>
                <div className="mt-3 min-w-0"><EventTimeline events={events} currentPartialText={{}} status={view.status} /></div>
              </details>
            </PanelCard>
          </WorkflowFloatingPanel>
          {spec ? (
            <WorkflowCanvas spec={spec} states={states} deadEdges={view.deadEdges} selectedStepId={selected} onSelectStep={toggleStep} className="absolute inset-0" collapsedLoops={collapsed} onCollapsedLoopsChange={setCollapsed} />
          ) : (
            <div className="absolute inset-0 flex items-center justify-center bg-background p-4">
              {!specError && !loadError ? <p className="text-label text-muted-foreground">Loading the workflow graph…</p> : null}
            </div>
          )}

          {layout.panel === "sheet" && (view.status === "failed" || view.status === "needs_attention") ? (
            // Below 768 px the run's alerts sit in the closed bottom Sheet: announce the outcome here.
            <p
              role="alert"
              data-testid="workflow-run-status-alert"
              data-status={view.status}
              style={{ top: CANVAS_INSET_PX, left: CANVAS_INSET_PX, right: CANVAS_INSET_PX }}
              className={`absolute z-10 truncate rounded-md border px-3 py-1.5 text-label shadow-raised ${view.status === "failed" ? "border-destructive/30 bg-destructive/10 text-destructive-text" : "border-warning bg-warning/10 text-warning-text"}`}
            >
              {view.status === "failed"
                ? `Failed${view.runError?.stepId ? ` at step ${view.runError.stepId}` : ""}: open the run details.`
                : "Needs attention: open the run details before running again."}
            </p>
          ) : null}

          {approvals || floatingDetail ? (
            <div
              data-testid="workflow-right-column"
              style={{ top: CANVAS_INSET_PX, right: CANVAS_INSET_PX, bottom: 56, left: rightLeft }}
              className="pointer-events-none absolute z-10 flex justify-end"
            >
              <div style={{ maxWidth: layout.stepDetail === "floating" ? STEP_PANEL_WIDTH : APPROVAL_OVERLAY_MAX_WIDTH }} className="flex max-h-full w-full min-w-0 flex-col gap-2 overflow-y-auto">
                {approvals}
                {floatingDetail}
              </div>
            </div>
          ) : null}
        </div>

        {spec ? (
          <TimelineDock
            rows={rows}
            open={prefs.timelineOpen}
            height={prefs.timelineHeight}
            onOpenChange={(open) => update({ timelineOpen: open })}
            onHeightChange={(height, persist) => update({ timelineHeight: height }, persist)}
            selectedStepId={selected}
            onSelectStep={toggleStep}
          />
        ) : null}
      </div>

      {selectedDetail && layout.stepDetail === "sheet" ? (
        <Sheet open onOpenChange={(open) => { if (!open) setSelected(null); }}>
          <SheetContent size="md" aria-describedby={undefined} data-testid="workflow-step-sheet">
            <SheetHeader title={`Step ${selectedDetail.stepId}`} />
            <SheetBody>
              <StepIoPanel step={selectedStep} detail={selectedDetail} onClose={() => setSelected(null)} className="min-w-0 space-y-3" />
            </SheetBody>
          </SheetContent>
        </Sheet>
      ) : null}
    </div>
  );
}
