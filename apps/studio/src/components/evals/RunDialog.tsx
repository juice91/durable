"use client";

import React, { useEffect, useId, useRef, useState } from "react";
import { Button } from "@/components/ui/Button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/Dialog";
import type { Agent, AgentVersion } from "@/lib/api";
import {
  DEFAULT_MAX_CONCURRENCY,
  MAX_MAX_CONCURRENCY,
  getWarnings,
  newRunId,
  startEvalRun,
  type EvalSuite,
  type EvalTarget,
  type SideEffectWarning,
  type StartedEvalRun,
} from "@/lib/evals";
import { knowledgeBasesOf } from "@/components/agents/knowledgeMemory";
import { KnowledgeNote, MemoryNote, WarningIcon, alertClass, fieldClass, hintClass, labelClass, warningClass } from "./evalsUi";

interface RunDialogProps {
  agent: Agent;
  versions: AgentVersion[];
  suite: EvalSuite;
  hasMemory: boolean;
  onStarted: (run: StartedEvalRun) => void;
  onClose: () => void;
}

/** The target select's value: "draft" or a version number as text. */
function targetValue(target: EvalTarget): string {
  return target === null ? "" : String(target);
}

function parseTarget(value: string): EvalTarget {
  if (value === "draft") return "draft";
  const n = Number(value);
  return Number.isInteger(n) && n >= 1 ? n : null;
}

/**
 * "Run suite": against a published version (the latest by default) or the draft. The tools that run
 * for real (built-ins with side effects and MCP tools not under approval) are listed, and the run
 * button says "Run anyway" while any are. The memory note shows for an agent with memory, and the
 * knowledge note (retrieval and its embedding cost per case) when the target's spec has knowledge bases.
 */
export function RunDialog({ agent, versions, suite, hasMemory, onStarted, onClose }: RunDialogProps) {
  const id = useId();
  const published = [...versions].sort((a, b) => b.version - a.version);
  const [target, setTarget] = useState<EvalTarget>(published.length > 0 ? published[0].version : "draft");
  const [concurrency, setConcurrency] = useState(DEFAULT_MAX_CONCURRENCY);
  const [warnings, setWarnings] = useState<SideEffectWarning[] | null>(null);
  const [warningsError, setWarningsError] = useState<string | null>(null);
  const [starting, setStarting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // One run id per dialog (and target): a retry after a network error reuses it, so the POST is idempotent.
  const runIdRef = useRef<{ key: string; id: string } | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    setWarnings(null);
    setWarningsError(null);
    getWarnings(agent.id, target, controller.signal)
      .then((data) => {
        if (!controller.signal.aborted) setWarnings(Array.isArray(data.warnings) ? data.warnings : []);
      })
      .catch((reason: unknown) => {
        if (controller.signal.aborted) return;
        setWarnings([]);
        setWarningsError(reason instanceof Error && reason.message ? reason.message : "The target's tools could not be checked");
      });
    return () => controller.abort();
  }, [agent.id, target]);

  const start = async (event: React.FormEvent) => {
    event.preventDefault();
    if (starting) return;
    setStarting(true);
    setError(null);
    const key = `${suite.id}:${targetValue(target)}:${concurrency}`;
    if (runIdRef.current?.key !== key) runIdRef.current = { key, id: newRunId() };
    try {
      const started = await startEvalRun(agent.id, { run_id: runIdRef.current.id, suite_id: suite.id, version: target, max_concurrency: concurrency });
      onStarted(started);
    } catch (reason: unknown) {
      setError(reason instanceof Error && reason.message ? reason.message : "The eval run could not be started");
      setStarting(false);
    }
  };

  // The target's spec: the draft, or the chosen published version.
  const targetSpec = target === "draft" ? agent.draft_spec : published.find((v) => v.version === target)?.spec ?? null;
  const hasKnowledge = knowledgeBasesOf(targetSpec).length > 0;
  const hasWarnings = (warnings?.length ?? 0) > 0;
  const noCases = suite.case_count === 0;

  return (
    <Dialog open onOpenChange={(open) => { if (!open && !starting) onClose(); }}>
      <DialogContent className="md:max-w-xl" data-testid="eval-run-dialog">
        <DialogHeader>
          <DialogTitle>Run {suite.name}</DialogTitle>
          <DialogDescription>
            Each of the {suite.case_count} {suite.case_count === 1 ? "case" : "cases"} runs as its own agent run; the results, scores and cost are recorded.
          </DialogDescription>
        </DialogHeader>
        <form onSubmit={(e) => void start(e)} className="min-w-0 space-y-4" aria-label="Run suite">
          {error ? <p role="alert" className={alertClass} data-testid="eval-run-error">{error}</p> : null}
          {noCases ? <p className={alertClass} data-testid="eval-run-no-cases">The suite has no cases yet.</p> : null}
          <div className="space-y-1">
            <label htmlFor={`${id}-target`} className={labelClass}>Run against</label>
            <select id={`${id}-target`} className={fieldClass} value={targetValue(target)} onChange={(e) => setTarget(parseTarget(e.target.value))} disabled={starting} data-testid="eval-run-target">
              {published.map((v, i) => (
                <option key={v.version} value={String(v.version)}>
                  v{v.version}
                  {i === 0 ? " (latest)" : ""}
                </option>
              ))}
              <option value="draft">Draft</option>
            </select>
            <p className={hintClass}>
              {target === "draft" ? "The unpublished draft, frozen when the run starts." : agent.latest_version === null ? "The agent has no published version." : "A published version."}
            </p>
          </div>
          <div className="space-y-1">
            <label htmlFor={`${id}-concurrency`} className={labelClass}>Cases at a time</label>
            <select id={`${id}-concurrency`} className={fieldClass} value={String(concurrency)} onChange={(e) => setConcurrency(Number(e.target.value))} disabled={starting} data-testid="eval-run-concurrency">
              {Array.from({ length: MAX_MAX_CONCURRENCY }, (_, i) => i + 1).map((n) => (
                <option key={n} value={String(n)}>{n}</option>
              ))}
            </select>
          </div>
          {hasMemory ? <MemoryNote /> : null}
          {hasKnowledge ? <KnowledgeNote /> : null}
          {warnings === null ? (
            <p className={hintClass} role="status">Checking the target&apos;s tools…</p>
          ) : hasWarnings ? (
            <div className={warningClass} role="alert" data-testid="eval-run-warnings">
              <p className="flex min-w-0 items-start gap-2 font-medium">
                <WarningIcon />
                <span className="min-w-0">These tools run for real in every case that calls them:</span>
              </p>
              <ul className="mt-2 list-disc space-y-1 pl-6">
                {warnings.map((w) => (
                  <li key={`${w.kind}:${w.tool}`} className="break-words" data-testid="eval-run-warning">
                    <span className="font-mono">{w.tool}</span> — {w.message}
                  </li>
                ))}
              </ul>
              <p className="mt-2">Gate a tool with “Require approval” in the Editor to have it denied during evaluation instead.</p>
            </div>
          ) : warningsError ? (
            <div className={warningClass} role="alert">
              <p className="flex min-w-0 items-start gap-2">
                <WarningIcon />
                <span className="min-w-0">
                  The target&apos;s tools could not be checked for side effects, so any of them may run for real:{" "}
                  <span data-testid="eval-run-warnings-error">{warningsError}</span>
                </span>
              </p>
            </div>
          ) : null}
          <DialogFooter>
            <Button type="button" variant="ghost" onClick={onClose} disabled={starting}>Cancel</Button>
            <Button
              type="submit"
              // Side-effect tools: destructive. An unchecked target: not the primary (safe-looking) style.
              variant={hasWarnings ? "destructive" : warningsError ? "secondary" : "primary"}
              disabled={starting || noCases || warnings === null}
              data-testid="eval-run-start"
            >
              {starting ? "Starting…" : hasWarnings ? "Run anyway" : "Run"}
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
