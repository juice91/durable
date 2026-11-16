"use client";

import React, { useEffect, useRef, useState } from "react";
import { ToolRunError } from "@/lib/toolRuns";
import type { Run } from "@/lib/runs";
import { startWorkflowRun } from "@/lib/workflows";
import { Button } from "@/components/ui/Button";

/** The input a past run was started with, or null when it cannot be read back as an object. */
export function parseRunInput(raw: string | null | undefined): Record<string, unknown> | null {
  try {
    const parsed: unknown = JSON.parse(raw ?? "");
    return parsed && typeof parsed === "object" && !Array.isArray(parsed) ? (parsed as Record<string, unknown>) : null;
  } catch {
    return null;
  }
}

export interface RerunButtonProps {
  run: Pick<Run, "workflow_id" | "workflow_version" | "input" | "kind">;
  /** Called with the NEW run's id once the server accepted it (the caller navigates to it). */
  onStarted: (runId: string) => void;
  size?: "sm" | "md";
}

/**
 * "Run again": starts a NEW run of the same workflow with the past run's input exactly and on the
 * same published version. The old run is never touched. While starting it is disabled; a failure
 * shows beside the button (the same banner text a run-start error shows in the run panel).
 */
export function RerunButton({ run, onStarted, size = "sm" }: RerunButtonProps) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const mounted = useRef(true);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  const input = parseRunInput(run.input);
  if (run.kind !== "workflow" || !run.workflow_id || run.workflow_version == null || input === null) return null;
  const workflowId = run.workflow_id;
  const version = run.workflow_version;

  const rerun = async () => {
    if (busy) return;
    setBusy(true);
    setError(null);
    try {
      const accepted = await startWorkflowRun(workflowId, input, { version });
      onStarted(accepted.run_id);
    } catch (reason: unknown) {
      if (mounted.current) setError(reason instanceof ToolRunError ? (reason.banner ?? reason.message) : reason instanceof Error ? reason.message : "Failed to start the workflow run");
    } finally {
      if (mounted.current) setBusy(false);
    }
  };

  return (
    <>
      <Button size={size} variant="secondary" data-testid="workflow-run-again" disabled={busy} aria-busy={busy} title={`Start a new run with the same input on version ${version}`} onClick={() => void rerun()}>
        {busy ? "Starting…" : "Run again"}
      </Button>
      {error ? <span role="alert" data-testid="workflow-run-again-error" className="text-caption text-destructive-text">{error}</span> : null}
    </>
  );
}
