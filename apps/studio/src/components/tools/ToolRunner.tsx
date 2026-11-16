"use client";

import React, { useEffect, useRef, useState } from "react";
import { Button } from "@/components/ui/Button";
import { cn } from "@/lib/cn";
import { ApiError } from "@/lib/api";
import type { RunStatus } from "@/lib/runs";
import { startToolRun, ToolRunError, type ToolRunAccepted } from "@/lib/toolRuns";
import { canonicalInputKey, toolRunLedger, type PendingRun } from "@/lib/toolRunLedger";
import type { ToolDefinition } from "@/lib/tools";
import { JsonSchemaForm } from "./JsonSchemaForm";
import { ToolRunResult } from "./ToolRunResult";

export const UNCERTAIN_MESSAGE = "The request may have reached the server; running again will reuse the same run id.";

/** True when the answer proves nothing was recorded for this run id, so it may be forgotten. */
function isDefinitiveRefusal(reason: unknown): boolean {
  if (reason instanceof ToolRunError) return reason.status >= 400 && reason.status < 500;
  // Missing credentials are detected before any request is sent.
  return reason instanceof ApiError && reason.status === 0;
}

type InputMode = "form" | "json";

/** Re-renders when the tool-run ledger changes; false until mounted (the server render has no storage). */
export function useToolRunLedger(): boolean {
  const [ready, setReady] = useState(false);
  const [, setVersion] = useState(0);
  useEffect(() => {
    setReady(true);
    return toolRunLedger.subscribe(() => setVersion((version) => version + 1));
  }, []);
  return ready;
}

/**
 * The Run tab: the input form (Form / JSON), Run, the error banner, the reuse hint and the result.
 *
 * Mount it with `key={tool.id}`: one instance runs one tool. Pending run ids live in `toolRunLedger`
 * (sessionStorage), outside this component, so a lost response, an unmount (closing the Sheet or
 * switching tools) or a reload never leads to a fresh run id for the same tool and input. A 202 that
 * arrives after this instance is gone becomes an "Open run" note instead. The request itself is
 * never aborted: the server may already have accepted the run.
 */
export function ToolRunner({
  tool,
  start,
  onStarted,
}: {
  tool: ToolDefinition;
  /** How a run is started; the built-in tools' POST /tools/{id}/runs by default (an MCP tool passes its own route). */
  start?: (input: Record<string, unknown>, runId: string) => Promise<ToolRunAccepted>;
  /** Called with the accepted run while this runner is still mounted (e.g. to refresh a history list). */
  onStarted?: (accepted: ToolRunAccepted) => void;
}) {
  const [mode, setMode] = useState<InputMode>("form");
  const [input, setInput] = useState<Record<string, unknown>>({});
  const [rawInput, setRawInput] = useState("{}");
  const [jsonError, setJsonError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [runBanner, setRunBanner] = useState<string | null>(null);
  const [fieldErrors, setFieldErrors] = useState<Record<string, string>>({});
  const [activeRun, setActiveRun] = useState<{ runId: string; status: RunStatus } | null>(null);
  const ledgerReady = useToolRunLedger();
  const requestGeneration = useRef(0);
  const mounted = useRef(true);

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  const pending: PendingRun | undefined = ledgerReady && !submitting ? toolRunLedger.pending(tool.id, canonicalInputKey(input)) : undefined;

  const runTool = async () => {
    if (submitting || (mode === "json" && jsonError)) return;
    const toolId = tool.id;
    const toolTitle = tool.title;
    const inputKey = canonicalInputKey(input);
    // Reuse the id of any earlier request for this tool and input whose outcome is unknown.
    const runId = toolRunLedger.pending(toolId, inputKey)?.runId ?? crypto.randomUUID();
    toolRunLedger.remember({ toolId, toolTitle, inputKey, runId, state: "in_flight" });
    const generation = ++requestGeneration.current;
    const isCurrent = () => mounted.current && requestGeneration.current === generation;
    setSubmitting(true);
    setRunBanner(null);
    setFieldErrors({});
    try {
      const accepted = await (start ? start(input, runId) : startToolRun(toolId, input, runId));
      // Ledger updates happen even after an unmount: they are what a remounted runner reads.
      toolRunLedger.forget(toolId, inputKey, runId);
      onStarted?.(accepted);
      if (isCurrent()) setActiveRun({ runId: accepted.run_id, status: accepted.display_status });
      else toolRunLedger.addNote({ runId: accepted.run_id, toolId, toolTitle });
    } catch (reason: unknown) {
      if (isDefinitiveRefusal(reason)) {
        toolRunLedger.forget(toolId, inputKey, runId);
      } else {
        const state = reason instanceof ToolRunError && reason.status === 503 ? "not_started" : "uncertain";
        toolRunLedger.remember({ toolId, toolTitle, inputKey, runId, state });
      }
      if (!isCurrent()) return;
      if (reason instanceof ToolRunError) {
        setRunBanner(reason.banner);
        setFieldErrors(reason.fieldErrors);
        if (Object.keys(reason.fieldErrors).length > 0 && mode === "json") setMode("form");
      } else if (isDefinitiveRefusal(reason)) {
        setRunBanner(reason instanceof Error ? reason.message : "Failed to start the tool run");
      } else {
        const detail = reason instanceof Error && reason.message ? `${reason.message}. ` : "";
        setRunBanner(`${detail}${UNCERTAIN_MESSAGE}`);
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
      setJsonError("Enter valid JSON");
    }
  };

  return (
    <div className="min-w-0 space-y-4" data-testid="tool-runner">
      <section className="min-w-0 space-y-3">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <h3 className="text-label font-semibold text-foreground">Input</h3>
          <div role="group" aria-label="Input mode" className="flex rounded-md border border-border p-0.5">
            {(["form", "json"] as const).map((item) => (
              <button
                key={item}
                type="button"
                aria-pressed={mode === item}
                onClick={() => setMode(item)}
                className={cn(
                  "rounded px-3 py-1 text-label focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
                  mode === item ? "bg-fill-active text-foreground" : "text-muted-foreground hover:bg-fill-hover hover:text-foreground",
                )}
              >
                {item === "form" ? "Form" : "JSON"}
              </button>
            ))}
          </div>
        </div>
        {mode === "form" ? <JsonSchemaForm schema={tool.input_schema} value={input} onChange={setFormInput} errors={fieldErrors} /> : (
          <div className="space-y-1">
            <label htmlFor="tool-input-json" className="sr-only">Tool input JSON</label>
            <textarea id="tool-input-json" rows={12} value={rawInput} onChange={(event) => setJsonInput(event.target.value)} className="w-full rounded-md border border-input bg-background p-3 font-mono text-body text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring" />
            {jsonError ? <p role="alert" className="text-caption text-destructive-text">{jsonError}</p> : null}
          </div>
        )}
        <div className="space-y-3 border-t border-border pt-4">
          {runBanner ? <p role="alert" data-testid="tool-run-banner" className="rounded-md border border-destructive/30 bg-destructive/10 p-3 text-body text-destructive-text">{runBanner}</p> : null}
          <div className="flex flex-wrap items-center gap-3">
            <Button variant="primary" onClick={runTool} disabled={submitting || (mode === "json" && Boolean(jsonError))} aria-busy={submitting}>Run</Button>
            {submitting ? <span className="text-caption text-muted-foreground" aria-live="polite">Starting…</span> : null}
            {pending ? (
              <span data-testid="tool-run-reuse-hint" data-state={pending.state} className="text-caption text-muted-foreground">
                {pending.state === "not_started" ? "Running again sends the same request (same run id)." : UNCERTAIN_MESSAGE}
              </span>
            ) : null}
            {!tool.allowed ? <span className="text-caption text-muted-foreground">This tool is not in the platform allowlist; the API will refuse the run.</span> : null}
            {tool.allowed && tool.side_effect ? <span className="text-caption text-muted-foreground">This tool has side effects; each Run performs it once.</span> : null}
          </div>
        </div>
      </section>
      {activeRun ? <ToolRunResult key={activeRun.runId} runId={activeRun.runId} initialStatus={activeRun.status} /> : null}
    </div>
  );
}
