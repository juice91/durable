"use client";

import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import Link from "next/link";
import { StatusPill } from "@/components/runs/StatusPill";
import { EventTimeline, formatValue } from "@/components/runs/EventTimeline";
import { ApiError } from "@/lib/api";
import { getRun, listEvents, subscribeRun, type Run, type RunEvent, type RunStatus } from "@/lib/runs";
import { deriveRunView, isTerminal, mergeEvents, type NodeState } from "@/lib/workflowRunState";
import { getWorkflowVersion, type WorkflowSpec } from "@/lib/workflows";
import { WorkflowGraph } from "./WorkflowGraph";
import { StepIoPanel } from "./StepIoPanel";
import { ApprovalCard, formatDateTime } from "./ApprovalCard";
import { formatDuration } from "./WorkflowRunHistory";

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
}

/**
 * The live execution view of one workflow run: the version's graph with node states from the run
 * stream, waiting approvals, and a step I/O panel. Events come from GET /events (backfill and
 * refresh) and the SSE stream; both are merged by seq, so duplicates and replays are harmless.
 */
export function WorkflowRunView({ runId, workflowId }: WorkflowRunViewProps) {
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
  const stepEventCount = events.filter((e) => e.type.startsWith("step_")).length;
  const selectedDetail = selected ? view.steps[selected] ?? null : null;
  const selectedStep = selected && spec ? spec.steps.find((s) => s.id === selected) ?? null : null;
  const effectiveWorkflowId = run?.workflow_id ?? workflowId ?? null;

  if (notFound) {
    return (
      <div role="alert" data-testid="workflow-run-not-found" className="rounded-md border border-destructive/30 bg-destructive/10 p-4 text-sm text-destructive">
        Run not found.
      </div>
    );
  }

  return (
    <div data-testid="workflow-run-view" data-status={view.status} className="space-y-5">
      <header className="flex flex-wrap items-center justify-between gap-3">
        <div className="space-y-1">
          <h1 className="text-2xl font-bold text-foreground">
            {spec?.name ?? "Workflow run"} <span className="font-mono text-base font-normal text-muted-foreground">{runId.slice(0, 8)}</span>
          </h1>
          <p className="text-sm text-muted-foreground">
            {run?.workflow_version != null ? `Version ${run.workflow_version} · ` : ""}
            Started {formatDateTime(run?.created_at)}
            {isTerminal(view.status) && run ? ` · took ${formatDuration(run.created_at, run.updated_at) ?? "—"}` : ""}
          </p>
        </div>
        <div className="flex items-center gap-3">
          <span data-testid="workflow-run-stream" data-state={stream} aria-live="polite" className="text-xs text-muted-foreground">{STREAM_LABELS[stream]}</span>
          <span data-testid="workflow-run-status" data-status={view.status}><StatusPill status={view.status} showRecoveringNotice /></span>
          {effectiveWorkflowId ? (
            <Link href={`/workflows/${encodeURIComponent(effectiveWorkflowId)}`} className="text-sm text-primary underline-offset-4 hover:underline">Back to workflow</Link>
          ) : null}
          <Link href={`/runs/${runId}`} className="text-sm text-primary underline-offset-4 hover:underline">Run details</Link>
        </div>
      </header>

      {workflowId && run?.workflow_id && run.workflow_id !== workflowId ? (
        <p role="status" className="rounded-md border border-border bg-muted/40 p-3 text-sm text-foreground">This run belongs to another workflow; showing that workflow&apos;s graph.</p>
      ) : null}
      {loadError ? <p role="alert" className="rounded-md border border-destructive/30 bg-destructive/10 p-3 text-sm text-destructive">{loadError}</p> : null}
      {specError ? <p role="alert" className="rounded-md border border-destructive/30 bg-destructive/10 p-3 text-sm text-destructive">Graph unavailable: {specError}</p> : null}

      {view.status === "needs_attention" ? (
        <div role="alert" data-testid="workflow-run-needs-attention" className="rounded-md border-2 border-orange-500 bg-orange-100/50 p-3 text-sm text-foreground dark:bg-orange-900/20">
          <p className="font-semibold">Needs attention: a step may have had side effects whose outcome is unknown.</p>
          <p className="mt-1 text-xs">It was not retried automatically. Check the target system before running the workflow again.</p>
          {view.runError?.error ? <p className="mt-1 whitespace-pre-wrap text-xs">{view.runError.error}</p> : null}
        </div>
      ) : null}
      {view.status === "failed" ? (
        <div role="alert" data-testid="workflow-run-failed" className="rounded-md border border-destructive/30 bg-destructive/10 p-3 text-sm text-destructive">
          <p className="font-semibold">Failed{view.runError?.stepId ? ` at step ${view.runError.stepId}` : ""}</p>
          {view.runError?.error ?? run?.error ? <p className="mt-1 whitespace-pre-wrap">{view.runError?.error ?? run?.error}</p> : null}
          {view.runError?.errorCode ? <p className="mt-1 text-xs">Error code: <code data-testid="workflow-run-error-code" className="font-mono">{view.runError.errorCode}</code></p> : null}
        </div>
      ) : null}
      {isTerminal(view.status) && stepEventCount === 0 ? (
        <p role="status" data-testid="workflow-run-no-steps" className="rounded-md border border-border bg-muted/40 p-3 text-sm text-foreground">
          The run ended ({view.status.replace("_", " ")}) before any step ran.
        </p>
      ) : null}
      {view.status === "completed" && view.output !== undefined ? (
        <details className="rounded-md border border-border bg-card p-3" open>
          <summary className="cursor-pointer text-sm font-semibold text-card-foreground">Output</summary>
          <pre data-testid="workflow-run-output" className="mt-2 max-h-64 overflow-auto whitespace-pre-wrap break-words rounded-md bg-muted p-2 font-mono text-xs text-foreground">{formatValue(view.output)}</pre>
        </details>
      ) : null}

      {view.waiting.length > 0 ? (
        <section aria-label="Waiting approvals" className="space-y-3">
          {view.waiting.map((id) => (
            <ApprovalCard key={id} runId={runId} stepId={id} prompt={view.steps[id]?.prompt ?? null} deadline={view.steps[id]?.deadline ?? null} onRefresh={refresh} />
          ))}
        </section>
      ) : null}

      {spec ? (
        <div className="grid gap-5 2xl:grid-cols-[minmax(0,1fr)_24rem]">
          <WorkflowGraph spec={spec} states={states} deadEdges={view.deadEdges} selectedStepId={selected} onSelectStep={(id) => setSelected((cur) => (cur === id ? null : id))} />
          {selectedDetail ? <StepIoPanel step={selectedStep} detail={selectedDetail} onClose={() => setSelected(null)} /> : (
            <p className="text-sm text-muted-foreground">Select a step to see its input and output.</p>
          )}
        </div>
      ) : !specError && !loadError ? <p className="text-sm text-muted-foreground">Loading the workflow graph…</p> : null}

      <details className="rounded-md border border-border bg-card p-3">
        <summary className="cursor-pointer text-sm font-semibold text-card-foreground">Events ({events.length})</summary>
        <div className="mt-3"><EventTimeline events={events} currentPartialText={{}} status={view.status} /></div>
      </details>
    </div>
  );
}
