"use client";

import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import Link from "next/link";
import { StatusPill } from "@/components/runs/StatusPill";
import { EventTimeline, formatValue } from "@/components/runs/EventTimeline";
import { ApiError } from "@/lib/api";
import { getRun, listEvents, subscribeRun, type Run, type RunEvent, type RunStatus } from "@/lib/runs";
import { deriveRunView, findStep, isTerminal, mergeEvents, resolveRunStates, type IterationSelection } from "@/lib/workflowRunState";
import { getWorkflowVersion, type WorkflowSpec } from "@/lib/workflows";
import { Copy } from "lucide-react";
import { ICON_STROKE_WIDTH } from "@/lib/icons";
import { navCrumb } from "@/lib/nav";
import { routes } from "@/lib/routes";
import { Badge } from "@/components/ui/Badge";
import { RecoveredBadge } from "@/components/runs/RecoveredBadge";
import { IconButton } from "@/components/ui/IconButton";
import { CopyableText } from "@/components/ui/CopyableText";
import { Sheet, SheetBody, SheetContent, SheetHeader } from "@/components/ui/Sheet";
import { toast } from "@/components/ui/Toaster";
import type { LinkTab } from "@/components/ui/Tabs";
import { PageHeader } from "@/components/frame/PageHeader";
import { TraceLink } from "@/components/traces/TraceLink";
import { WorkflowCanvas, WorkflowStepList } from "./WorkflowGraph";
import { StepIoPanel } from "./StepIoPanel";
import { useWorkflowExplanation } from "@/lib/workflowExplanations";
import { ApprovalCard, formatDateTime } from "@/components/approvals/ApprovalCard";
import { formatRunDuration } from "./WorkflowRunHistory";
import { listWorkflowRuns } from "@/lib/workflows";
import { listSchedules } from "@/lib/schedules";
import { PanelCard, useWorkflowLayout, WorkflowFloatingPanel } from "./WorkflowFloatingPanel";
import { TimelineDock } from "./TimelineDock";
import { CANVAS_INSET_PX, STEP_PANEL_WIDTH } from "./workflowLayout";
import { timelineClock, timelineRows } from "./workflowTimeline";
import { CancelRunButton } from "./CancelRunButton";
import { RerunButton } from "./RerunButton";
import { PausedBanner, pausedAfter } from "./PausedBanner";
import { IterationPager, LoopIterationTable } from "./LoopIterations";
import { copyText } from "@/lib/clipboard";
import type { JsonSchema } from "@/lib/tools";

/** How often a live run's open timeline bars grow. */
const TIMELINE_TICK_MS = 1000;

/** The approvals overlay's widest, when no step detail shares the right column. */
const APPROVAL_OVERLAY_MAX_WIDTH = 416;

type StreamState = "connecting" | "live" | "reconnecting" | "ended";

/** "each iteration 4 › inner iteration 1" for the scope string `each.3/inner.0`. */
export function iterationScopeLabel(scope: string): string {
  return scope
    .split("/")
    .map((frame) => {
      const dot = frame.lastIndexOf(".");
      return `${frame.slice(0, dot)} iteration ${Number(frame.slice(dot + 1)) + 1}`;
    })
    .join(" › ");
}

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
  /** The iteration pagers' choices by loop instance key (the page's `?iter=` state); omitted, kept here. */
  iterationSelection?: IterationSelection;
  onIterationSelectionChange?: (next: IterationSelection) => void;
  /** Loop instances whose pager steps through failed iterations only (`?failedOnly=`). */
  failedOnlyLoops?: ReadonlySet<string>;
  onFailedOnlyLoopsChange?: (next: Set<string>) => void;
  /** "Run again" started a new run (its id); the page navigates to it. Omitted, no button. */
  onRerunStarted?: (runId: string) => void;
}

/** The "Cancelled" label of a run that ended with `run_cancelled` and status `failed`. (A cancel that
 * ended `needs_attention`, a side effect still reserved, keeps the Needs attention pill instead.) */
function CancelledPill() {
  return (
    <span data-testid="workflow-run-cancelled" className="inline-flex items-center rounded-full bg-muted px-3 py-1 text-label font-medium text-foreground">
      Cancelled
    </span>
  );
}

/**
 * The live execution view of one workflow run (SL.14): the version's graph, full-bleed, with node
 * states from the run stream; a floating panel with the run's status, duration, id and the step
 * list; waiting approvals as a "Needs input" overlay; the selected step's I/O in a right column (a
 * Sheet where it does not fit); and the bottom Timeline dock. Events come from GET /events
 * (backfill and refresh) and the SSE stream; both are merged by seq, so duplicates and replays are
 * harmless.
 */
export function WorkflowRunView({
  runId,
  workflowId,
  tabs,
  collapsedLoops,
  onCollapsedLoopsChange,
  iterationSelection,
  onIterationSelectionChange,
  failedOnlyLoops,
  onFailedOnlyLoopsChange,
  onRerunStarted,
}: WorkflowRunViewProps) {
  // One collapsed set for the canvas and the step list: the page's URL state, else kept here.
  const [ownCollapsed, setOwnCollapsed] = useState<ReadonlySet<string>>(() => new Set());
  const collapsed = collapsedLoops ?? ownCollapsed;
  const setCollapsed = onCollapsedLoopsChange ?? setOwnCollapsed;
  const [run, setRun] = useState<Run | null>(null);
  const [spec, setSpec] = useState<WorkflowSpec | null>(null);
  // The cached AI explanation of the run's workflow version: each step's note shows in its panel (never generated here).
  const aiNotes = useWorkflowExplanation(run?.workflow_id ?? "", run?.workflow_version ?? null).notes;
  const [events, setEvents] = useState<RunEvent[]>([]);
  const [status, setStatus] = useState<RunStatus>("queued");
  const [loadError, setLoadError] = useState<string | null>(null);
  const [specError, setSpecError] = useState<string | null>(null);
  const [notFound, setNotFound] = useState(false);
  const [stream, setStream] = useState<StreamState>("connecting");
  const [selected, setSelected] = useState<string | null>(null);
  // The iteration pager's choice per loop id (absent: the latest), and its "Failed only" toggles.
  // Controlled by the page's URL state when given (`?iter=`, `?failedOnly=`), else kept here.
  const [ownChoice, setOwnChoice] = useState<IterationSelection>({});
  const [ownFailedOnly, setOwnFailedOnly] = useState<ReadonlySet<string>>(() => new Set());
  const iterationChoice = iterationSelection ?? ownChoice;
  const setIterationChoice = onIterationSelectionChange ?? setOwnChoice;
  const failedOnly = failedOnlyLoops ?? ownFailedOnly;
  const setFailedOnly = onFailedOnlyLoopsChange ?? setOwnFailedOnly;
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
    setOwnChoice({});
    setOwnFailedOnly(new Set());

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
  // Body steps show their loop's chosen iteration (else the latest): one resolution for the graph,
  // the step list, the Timeline and the step detail.
  const resolved = useMemo(() => resolveRunStates(spec, view, iterationChoice), [spec, view, iterationChoice]);
  const states = resolved.states;
  const selectIteration = useCallback(
    (key: string, index: number | null) => {
      const next = { ...iterationChoice };
      if (index === null) delete next[key];
      else next[key] = index;
      setIterationChoice(next);
    },
    [iterationChoice, setIterationChoice],
  );
  const setLoopFailedOnly = useCallback(
    (key: string, on: boolean) => {
      const next = new Set(failedOnly);
      if (on) next.add(key);
      else next.delete(key);
      setFailedOnly(next);
    },
    [failedOnly, setFailedOnly],
  );
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
  const rows = useMemo(() => timelineRows(spec, view, clock, resolved), [spec, view, clock, resolved]);
  const stepEventCount = events.filter((e) => e.type.startsWith("step_")).length;
  const selectedDetail = selected ? resolved.details[selected] ?? view.steps[selected] ?? null : null;
  const selectedStep = selected ? findStep(spec, selected) : null;
  const selectedScope = selected ? resolved.scopes[selected] ?? null : null;
  const iterationLabel = selectedScope ? `From ${iterationScopeLabel(selectedScope)}` : null;
  const selectedPager = selected ? resolved.pagers.find((p) => p.loopId === selected) ?? null : null;
  const loopExtras = selectedPager ? <LoopIterationTable pager={selectedPager} onSelect={selectIteration} /> : null;
  const canCancel = run?.kind === "workflow" && !isTerminal(view.status);
  // A cancel that ended needs_attention (a side effect still reserved) keeps the warning first.
  const cancelledAttention = view.cancelled && view.status === "needs_attention";
  const cancelledPlain = view.cancelled && !cancelledAttention;
  const cancelledSecondary = cancelledAttention ? (
    <span data-testid="workflow-run-cancelled-secondary" className="text-caption text-muted-foreground">cancelled</span>
  ) : null;
  const effectiveWorkflowId = run?.workflow_id ?? workflowId ?? null;
  // The newest later run that re-ran this one, from the workflow's run history (no extra API).
  // The "Scheduled: <name>" lookup reads the first 200 schedules (one request, no paging loop); a deleted
  // or uncached schedule leaves the name null and the tag reads plain "Scheduled".
  const [scheduleName, setScheduleName] = useState<string | null>(null);
  const scheduleId = run?.schedule_id ?? null;
  useEffect(() => {
    setScheduleName(null);
    if (!scheduleId || !effectiveWorkflowId) return;
    const controller = new AbortController();
    listSchedules({ workflowId: effectiveWorkflowId, limit: 200, signal: controller.signal })
      .then((page) => {
        if (!controller.signal.aborted) setScheduleName(page.items.find((s) => s.id === scheduleId)?.name ?? null);
      })
      .catch(() => undefined);
    return () => controller.abort();
  }, [scheduleId, effectiveWorkflowId]);
  const [rerunId, setRerunId] = useState<string | null>(null);
  useEffect(() => {
    setRerunId(null);
    if (!effectiveWorkflowId) return;
    let active = true;
    listWorkflowRuns(effectiveWorkflowId)
      .then((page) => {
        if (!active) return;
        const newest = (page?.items ?? [])
          .filter((r) => r.rerun_of === runId)
          .reduce<Run | null>((a, r) => (a === null || r.created_at > a.created_at ? r : a), null);
        setRerunId(newest?.run_id ?? null);
      })
      .catch(() => {});
    return () => { active = false; };
  }, [effectiveWorkflowId, runId]);
  const control = useWorkflowLayout();
  const { layout, prefs, update } = control;
  const toggleStep = useCallback((id: string) => setSelected((cur) => (cur === id ? null : id)), []);

  // After Approve or Reject the decided card (often the whole overlay) goes away with the focus in
  // it: send focus to the run status rather than leave it on <body>.
  const statusRef = useRef<HTMLSpanElement>(null);
  const refocusAfterDecision = useRef(false);
  const waitingKey = view.waiting.join("\n");
  // A step-by-step pause (`<step>_pause`) is a banner, and opens the step it follows in the IO panel.
  const pausedId = view.waiting.find((id) => view.steps[id]?.pause) ?? null;
  useEffect(() => {
    if (pausedId) setSelected(pausedAfter(pausedId));
  }, [pausedId]);
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
          <span ref={statusRef} tabIndex={-1} data-testid="workflow-run-status" data-status={view.status} data-cancelled={view.cancelled} className="rounded-sm outline-none focus-visible:ring-2 focus-visible:ring-ring">
            {cancelledPlain ? <CancelledPill /> : (
              <span className="inline-flex items-center gap-2">
                <StatusPill status={view.status} showRecoveringNotice />
                {cancelledSecondary}
              </span>
            )}
          </span>
          <RecoveredBadge run={run} />
          {canCancel ? <CancelRunButton runId={runId} cancelRequested={view.cancelRequested} onSettled={() => void refresh()} /> : null}
          {run && onRerunStarted && isTerminal(view.status) ? <RerunButton run={run} onStarted={onRerunStarted} /> : null}
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
          {view.waiting.every((id) => view.steps[id]?.pause)
            ? "Paused. Continue or stop the run."
            : view.waiting.length === 1 ? "A step is waiting for approval." : `${view.waiting.length} steps are waiting for approval.`}
        </span>
      </p>
      {view.waiting.map((id) => view.steps[id]?.pause ? (
        <PausedBanner
          key={id}
          runId={runId}
          stepId={id}
          deadline={view.steps[id]?.deadline ?? null}
          onRefresh={refresh}
          onDecided={() => {
            refocusAfterDecision.current = true;
          }}
        />
      ) : (
        <ApprovalCard
          key={id}
          approval={{ kind: "step", runId, stepId: id, prompt: view.steps[id]?.prompt ?? null, deadline: view.steps[id]?.deadline ?? null, resumeSchema: (view.steps[id]?.resumeSchema as JsonSchema | null | undefined) ?? null }}
          onRefresh={refresh}
          onDecided={() => {
            refocusAfterDecision.current = true;
          }}
        />
      ))}
    </section>
  ) : null;
  const floatingDetail = selectedDetail && layout.stepDetail === "floating" ? (
    <StepIoPanel aiNote={selectedStep ? aiNotes.get(selectedStep.id) ?? null : null} step={selectedStep} detail={selectedDetail} iterationLabel={iterationLabel} iterations={selectedPager?.iterations} onClose={() => setSelected(null)} className="pointer-events-auto min-w-0 space-y-3 rounded-lg border border-border bg-surface-raised p-4 shadow-overlay">{loopExtras}</StepIoPanel>
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
                <dd className="flex min-w-0 flex-wrap items-center gap-2">{cancelledPlain ? <CancelledPill /> : <><StatusPill status={view.status} />{cancelledSecondary}</>}</dd>
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
                {scheduleId ? (
                  <p data-testid="workflow-run-scheduled" className="text-label text-muted-foreground">{scheduleName ? `Scheduled: ${scheduleName}` : "Scheduled"}</p>
                ) : null}
                {effectiveWorkflowId && run?.rerun_of ? (
                  <p data-testid="workflow-run-rerun-of" className="text-label text-muted-foreground">
                    Re-run of{" "}
                    <Link href={routes.workflowRun(effectiveWorkflowId, run.rerun_of)} className="rounded-sm font-mono text-info-text underline-offset-4 outline-none hover:underline focus-visible:ring-2 focus-visible:ring-ring">{run.rerun_of.slice(0, 8)}</Link>
                  </p>
                ) : null}
                {effectiveWorkflowId && rerunId ? (
                  <p data-testid="workflow-run-rerun-as" className="text-label text-muted-foreground">
                    Re-run as{" "}
                    <Link href={routes.workflowRun(effectiveWorkflowId, rerunId)} className="rounded-sm font-mono text-info-text underline-offset-4 outline-none hover:underline focus-visible:ring-2 focus-visible:ring-ring">{rerunId.slice(0, 8)}</Link>
                  </p>
                ) : null}
                {loadError ? <CopyableText label="error" copyText={loadError}><p role="alert" className="rounded-md border border-destructive/30 bg-destructive/10 p-2 pr-8 text-label text-destructive-text">{loadError}</p></CopyableText> : null}
                {specError ? <CopyableText label="error" copyText={`Graph unavailable: ${specError}`}><p role="alert" className="rounded-md border border-destructive/30 bg-destructive/10 p-2 pr-8 text-label text-destructive-text">Graph unavailable: {specError}</p></CopyableText> : null}
                {view.status === "needs_attention" ? (
                  <div role="alert" data-testid="workflow-run-needs-attention" className="rounded-md border-2 border-warning bg-warning/10 p-2 text-label text-foreground">
                    <p className="font-semibold">Needs attention: a step may have had side effects whose outcome is unknown.</p>
                    <p className="mt-1 text-caption">It was not retried automatically. Check the target system before running the workflow again.</p>
                    {cancelledAttention ? (
                      <p data-testid="workflow-run-needs-attention-cancelled" className="mt-1 text-caption">
                        The run was cancelled{view.cancelRequestedBy ? <> by <span className="font-mono">{view.cancelRequestedBy}</span></> : null} while that side effect was still in progress.
                      </p>
                    ) : null}
                    {view.runError?.error ? <p className="mt-1 whitespace-pre-wrap text-caption">{view.runError.error}</p> : null}
                  </div>
                ) : null}
                {cancelledPlain ? (
                  <div role="status" data-testid="workflow-run-cancelled-notice" className="rounded-md border border-border bg-muted/40 p-2 text-label text-foreground">
                    <p className="font-semibold">Cancelled{view.cancelRequestedBy ? <> by <span className="font-mono">{view.cancelRequestedBy}</span></> : null}</p>
                    <p className="mt-1 text-caption text-muted-foreground">Steps that were still running stopped. Finished steps keep their results.</p>
                  </div>
                ) : null}
                {view.status === "failed" && !view.cancelled ? (
                  <div role="alert" data-testid="workflow-run-failed" className="rounded-md border border-destructive/30 bg-destructive/10 p-2 text-label text-destructive-text">
                    <CopyableText
                      label="error"
                      className="pr-8"
                      copyText={[`Failed${view.runError?.stepId ? ` at step ${view.runError.stepId}` : ""}`, view.runError?.error ?? run?.error, view.runError?.errorCode ? `Error code: ${view.runError.errorCode}` : null].filter(Boolean).join("\n")}
                    >
                      <p className="font-semibold">Failed{view.runError?.stepId ? ` at step ${view.runError.stepId}` : ""}</p>
                      {view.runError?.error ?? run?.error ? <p className="mt-1 whitespace-pre-wrap">{view.runError?.error ?? run?.error}</p> : null}
                      {view.runError?.errorCode ? <p className="mt-1 text-caption">Error code: <code data-testid="workflow-run-error-code" className="font-mono">{view.runError.errorCode}</code></p> : null}
                    </CopyableText>
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

            {resolved.pagers.length > 0 ? (
              <PanelCard title="Loops" titleId="workflow-run-loops-heading" testId="workflow-run-loops">
                <p className="mb-2 text-caption text-muted-foreground">The graph, the step list and the Timeline show the selected iteration of each loop.</p>
                <div className="space-y-2">
                  {resolved.pagers.map((pager) => (
                    <IterationPager key={pager.key} pager={pager} failedOnly={failedOnly.has(pager.key)} onFailedOnlyChange={setLoopFailedOnly} onSelect={selectIteration} />
                  ))}
                </div>
              </PanelCard>
            ) : null}

            {spec ? (
              <PanelCard title="Steps" titleId="workflow-run-steps-heading">
                <p className="mb-2 text-caption text-muted-foreground">Select a step to see its input and output.</p>
                <WorkflowStepList spec={spec} states={states} selectedStepId={selected} onSelectStep={toggleStep} legend details={resolved.details} foldNotReached collapsedLoops={collapsed} onCollapsedLoopsChange={setCollapsed} />
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
            <WorkflowCanvas spec={spec} states={states} deadEdges={resolved.deadEdges} details={resolved.details} loopBadges={resolved.badges} selectedStepId={selected} onSelectStep={toggleStep} className="absolute inset-0" collapsedLoops={collapsed} onCollapsedLoopsChange={setCollapsed} />
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
              data-cancelled={view.cancelled}
              className={`absolute z-10 truncate rounded-md border px-3 py-1.5 text-label shadow-raised ${cancelledPlain ? "border-border bg-surface-raised text-foreground" : view.status === "failed" ? "border-destructive/30 bg-destructive/10 text-destructive-text" : "border-warning bg-warning/10 text-warning-text"}`}
            >
              {cancelledPlain
                ? "Cancelled: open the run details."
                : cancelledAttention
                ? "Needs attention (cancelled): open the run details before running again."
                : view.status === "failed"
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
            runId={runId}
            rows={rows}
            open={prefs.timelineOpen}
            height={prefs.timelineHeight}
            onOpenChange={(open) => update({ timelineOpen: open })}
            onHeightChange={(height, persist) => update({ timelineHeight: height }, persist)}
            selectedStepId={selected}
            onSelectStep={toggleStep}
            onSelectIteration={selectIteration}
          />
        ) : null}
      </div>

      {selectedDetail && layout.stepDetail === "sheet" ? (
        <Sheet open onOpenChange={(open) => { if (!open) setSelected(null); }}>
          <SheetContent size="md" aria-describedby={undefined} data-testid="workflow-step-sheet">
            <SheetHeader title={`Step ${selectedDetail.stepId}`} />
            <SheetBody>
              <StepIoPanel aiNote={selectedStep ? aiNotes.get(selectedStep.id) ?? null : null} step={selectedStep} detail={selectedDetail} iterationLabel={iterationLabel} iterations={selectedPager?.iterations} onClose={() => setSelected(null)} className="min-w-0 space-y-3">{loopExtras}</StepIoPanel>
            </SheetBody>
          </SheetContent>
        </Sheet>
      ) : null}
    </div>
  );
}
