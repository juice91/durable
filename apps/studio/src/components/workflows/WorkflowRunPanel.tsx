"use client";

import React, { useEffect, useRef, useState } from "react";
import Link from "next/link";
import { JsonSchemaForm } from "@/components/tools/JsonSchemaForm";
import { ApiError } from "@/lib/api";
import { canonicalInputKey, type PendingRun } from "@/lib/toolRunLedger";
import { ToolRunError } from "@/lib/toolRuns";
import { startWorkflowRun, workflowRunLedger, type WorkflowSpec } from "@/lib/workflows";
import { routes } from "@/lib/routes";
import { Switch } from "@/components/ui/Switch";

export const UNCERTAIN_MESSAGE = "The request may have reached the server; running again will reuse the same run id.";

/** True when the answer proves nothing was recorded for this run id, so it may be forgotten. */
function isDefinitiveRefusal(reason: unknown): boolean {
  if (reason instanceof ToolRunError) return reason.status >= 400 && reason.status < 500;
  return reason instanceof ApiError && reason.status === 0;
}

type InputMode = "form" | "json";

interface WorkflowRunPanelProps {
  workflowId: string;
  workflowName: string;
  /** The published version to run; null when the workflow has none (Run is disabled). */
  version: number | null;
  spec: WorkflowSpec | null;
  /** Called with the accepted run id while this panel is still showing the same workflow. */
  onStarted: (runId: string) => void;
  /** `card`: a bordered card; `panel` (default): borderless, a card of the floating panel. */
  variant?: "card" | "panel";
}

/**
 * Form/JSON input generated from the version's input_schema, and Run. Same run id semantics as a
 * tool run: the id is recorded per (workflow, input, version) before the request is sent, and a
 * lost or 5xx answer keeps it, so running again attaches to the run the server may already have.
 * Nothing is ever retried automatically.
 */
export function WorkflowRunPanel({ workflowId, workflowName, version, spec, onStarted, variant = "panel" }: WorkflowRunPanelProps) {
  const [mode, setMode] = useState<InputMode>("form");
  const [input, setInput] = useState<Record<string, unknown>>({});
  const [rawInput, setRawInput] = useState("{}");
  const [jsonError, setJsonError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [banner, setBanner] = useState<string | null>(null);
  const [stepByStep, setStepByStep] = useState(false);
  const [fieldErrors, setFieldErrors] = useState<Record<string, string>>({});
  const [ledgerReady, setLedgerReady] = useState(false);
  const [, setLedgerVersion] = useState(0);
  const generation = useRef(0);
  const mounted = useRef(true);
  const onStartedRef = useRef(onStarted);
  onStartedRef.current = onStarted;

  useEffect(() => {
    mounted.current = true;
    setLedgerReady(true);
    const unsubscribe = workflowRunLedger.subscribe(() => setLedgerVersion((v) => v + 1));
    return () => {
      mounted.current = false;
      unsubscribe();
    };
  }, []);

  // Another version (or workflow) is another form: a late answer for the old one becomes a note.
  useEffect(() => {
    generation.current += 1;
    setSubmitting(false);
    setInput({});
    setRawInput("{}");
    setJsonError(null);
    setBanner(null);
    setFieldErrors({});
  }, [workflowId, version]);

  const inputKey = canonicalInputKey({ input, version });
  const pending: PendingRun | undefined = ledgerReady && !submitting ? workflowRunLedger.pending(workflowId, inputKey) : undefined;
  const notes = ledgerReady ? workflowRunLedger.notes().filter((n) => n.toolId === workflowId) : [];

  const run = async () => {
    if (version == null || submitting || (mode === "json" && jsonError)) return;
    const key = canonicalInputKey({ input, version });
    const runId = workflowRunLedger.pending(workflowId, key)?.runId ?? crypto.randomUUID();
    workflowRunLedger.remember({ toolId: workflowId, toolTitle: workflowName, inputKey: key, runId, state: "in_flight" });
    const mine = ++generation.current;
    const isCurrent = () => mounted.current && generation.current === mine;
    setSubmitting(true);
    setBanner(null);
    setFieldErrors({});
    try {
      const accepted = await startWorkflowRun(workflowId, input, { version, runId, pauseAfterEachStep: stepByStep });
      workflowRunLedger.forget(workflowId, key, runId);
      if (isCurrent()) onStartedRef.current(accepted.run_id);
      else workflowRunLedger.addNote({ runId: accepted.run_id, toolId: workflowId, toolTitle: workflowName });
    } catch (reason: unknown) {
      if (isDefinitiveRefusal(reason)) {
        workflowRunLedger.forget(workflowId, key, runId);
      } else {
        const state = reason instanceof ToolRunError && reason.status === 503 ? "not_started" : "uncertain";
        workflowRunLedger.remember({ toolId: workflowId, toolTitle: workflowName, inputKey: key, runId, state });
      }
      if (!isCurrent()) return;
      if (reason instanceof ToolRunError) {
        setBanner(reason.banner);
        setFieldErrors(reason.fieldErrors);
        if (Object.keys(reason.fieldErrors).length > 0 && mode === "json") setMode("form");
      } else if (isDefinitiveRefusal(reason)) {
        setBanner(reason instanceof Error ? reason.message : "Failed to start the workflow run");
      } else {
        const detail = reason instanceof Error && reason.message ? `${reason.message}. ` : "";
        setBanner(`${detail}${UNCERTAIN_MESSAGE}`);
      }
    } finally {
      if (isCurrent()) setSubmitting(false);
    }
  };

  const setFormInput = (next: Record<string, unknown>) => {
    setInput(next);
    setRawInput(JSON.stringify(next, null, 2));
  };

  const setJsonInput = (raw: string) => {
    setRawInput(raw);
    try {
      const parsed: unknown = JSON.parse(raw);
      if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) throw new Error();
      setInput(parsed as Record<string, unknown>);
      setJsonError(null);
    } catch {
      setJsonError("Enter a valid JSON object");
    }
  };

  return (
    <section
      aria-labelledby="workflow-run-heading"
      data-testid="workflow-run-panel"
      className={variant === "panel" ? "min-w-0 border-b border-border p-4 last:border-b-0" : "min-w-0 rounded-lg border border-border bg-surface p-4"}
    >
      <div className="mb-3 flex min-w-0 flex-wrap items-center justify-between gap-2">
        <h2 id="workflow-run-heading" className="text-label font-semibold text-foreground">
          {/* A plain space, not a hidden ", " glued after "Run": the name read "Run , version 1". */}
          Run{version != null ? <>{" "}<span className="ml-1 text-caption font-normal text-muted-foreground">(version {version})</span></> : null}
        </h2>
        <div role="group" aria-label="Input mode" className="flex rounded-md border border-border p-0.5">
          {(["form", "json"] as const).map((item) => (
            <button key={item} type="button" aria-pressed={mode === item} onClick={() => setMode(item)} className={`rounded-sm px-2.5 py-0.5 text-label outline-none focus-visible:ring-2 focus-visible:ring-ring ${mode === item ? "bg-primary text-primary-foreground" : "text-muted-foreground hover:bg-fill-hover"}`}>
              {item === "form" ? "Form" : "JSON"}
            </button>
          ))}
        </div>
      </div>
      {notes.length > 0 ? (
        <ul data-testid="workflow-runs-started-elsewhere" aria-label="Runs started while you were away" className="mb-4 space-y-2">
          {notes.map((item) => (
            <li key={item.runId} role="status" className="flex flex-wrap items-center gap-2 rounded-md border border-border bg-muted/40 p-3 text-sm text-foreground">
              <span>A run of {item.toolTitle} was started:</span>
              <Link href={routes.workflowRun(workflowId, item.runId)} className="text-primary underline-offset-4 hover:underline">Open run</Link>
              <button type="button" aria-label={`Dismiss note for run ${item.runId}`} onClick={() => workflowRunLedger.dismissNote(item.runId)} className="ml-auto text-xs text-muted-foreground hover:text-foreground">Dismiss</button>
            </li>
          ))}
        </ul>
      ) : null}
      {spec ? (
        mode === "form" ? (
          <JsonSchemaForm schema={spec.input_schema} value={input} onChange={setFormInput} errors={fieldErrors} />
        ) : (
          <div className="space-y-1">
            <label htmlFor="workflow-input-json" className="sr-only">Workflow input JSON</label>
            <textarea id="workflow-input-json" data-testid="workflow-input-json" rows={10} value={rawInput} onChange={(event) => setJsonInput(event.target.value)} className="w-full rounded-md border border-input bg-background p-3 font-mono text-sm text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring" />
            {jsonError ? <p role="alert" className="text-xs text-destructive-text">{jsonError}</p> : null}
          </div>
        )
      ) : null}
      <div className="mt-4 space-y-3 border-t border-border pt-3">
        {banner ? <p role="alert" data-testid="workflow-run-banner" className="rounded-md border border-destructive/30 bg-destructive/10 p-3 text-sm text-destructive-text">{banner}</p> : null}
        <div className="flex items-start gap-3">
          <Switch id="workflow-step-by-step" checked={stepByStep} onCheckedChange={setStepByStep} aria-describedby="workflow-step-by-step-help" className="mt-0.5" />
          <div className="min-w-0">
            <label htmlFor="workflow-step-by-step" className="text-label font-medium text-foreground">Step by step</label>
            <p id="workflow-step-by-step-help" className="text-caption text-muted-foreground">Pause to inspect outputs</p>
          </div>
        </div>
        <div className="flex flex-wrap items-center gap-3">
          <button
            type="button"
            onClick={run}
            disabled={version == null || submitting || (mode === "json" && Boolean(jsonError))}
            aria-busy={submitting}
            className="rounded-md bg-primary px-3 py-1.5 text-label font-medium text-primary-foreground outline-none hover:bg-primary/90 focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background disabled:cursor-not-allowed disabled:opacity-50"
          >
            Run workflow
          </button>
          {submitting ? <span className="text-xs text-muted-foreground" aria-live="polite">Starting…</span> : null}
          {version == null ? <span className="text-xs text-muted-foreground">Publish a version to run this workflow.</span> : null}
          {pending ? (
            <span data-testid="workflow-run-reuse-hint" data-state={pending.state} className="text-xs text-muted-foreground">
              {pending.state === "not_started" ? "Running again sends the same request (same run id)." : UNCERTAIN_MESSAGE}
            </span>
          ) : null}
        </div>
      </div>
    </section>
  );
}
