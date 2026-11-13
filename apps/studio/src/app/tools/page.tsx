"use client";

import React, { useEffect, useMemo, useRef, useState } from "react";
import Link from "next/link";
import { JsonSchemaForm } from "@/components/tools/JsonSchemaForm";
import { ToolRunResult } from "@/components/tools/ToolRunResult";
import type { RunStatus } from "@/lib/runs";
import { listTools, type ToolDefinition } from "@/lib/tools";
import { ApiError } from "@/lib/api";
import { startToolRun, ToolRunError } from "@/lib/toolRuns";
import { canonicalInputKey, toolRunLedger, type PendingRun, type StartedRunNote } from "@/lib/toolRunLedger";

const UNCERTAIN_MESSAGE = "The request may have reached the server; running again will reuse the same run id.";

/** True when the answer proves nothing was recorded for this run id, so it may be forgotten. */
function isDefinitiveRefusal(reason: unknown): boolean {
  if (reason instanceof ToolRunError) return reason.status >= 400 && reason.status < 500;
  // Missing credentials are detected before any request is sent.
  return reason instanceof ApiError && reason.status === 0;
}

type InputMode = "form" | "json";

export default function ToolsPage() {
  const [tools, setTools] = useState<ToolDefinition[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [mode, setMode] = useState<InputMode>("form");
  const [input, setInput] = useState<Record<string, unknown>>({});
  const [rawInput, setRawInput] = useState("{}");
  const [jsonError, setJsonError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [runBanner, setRunBanner] = useState<string | null>(null);
  const [fieldErrors, setFieldErrors] = useState<Record<string, string>>({});
  const [activeRun, setActiveRun] = useState<{ runId: string; status: RunStatus } | null>(null);
  // Pending run ids and "Open run" notes live in toolRunLedger (sessionStorage), outside this
  // component, so a lost response, an unmount or a reload never leads to a fresh run id for the
  // same tool and input. They are read after mount only (the server render has no storage).
  const [ledgerReady, setLedgerReady] = useState(false);
  const [, setLedgerVersion] = useState(0);
  // Each start request gets a generation; selecting another tool bumps it, so a late answer for the
  // previous tool does not land in the new tool's panel. The request itself is never aborted:
  // the server may already have accepted the run.
  const requestGeneration = useRef(0);
  const mounted = useRef(true);

  useEffect(() => {
    mounted.current = true;
    setLedgerReady(true);
    const unsubscribe = toolRunLedger.subscribe(() => setLedgerVersion((version) => version + 1));
    return () => {
      mounted.current = false;
      unsubscribe();
    };
  }, []);


  useEffect(() => {
    listTools().then((catalog) => {
      setTools(catalog);
      setSelectedId(catalog[0]?.id ?? null);
    }).catch((reason: unknown) => setError(reason instanceof Error ? reason.message : "Failed to load tools"))
      .finally(() => setLoading(false));
  }, []);

  const selected = tools.find((tool) => tool.id === selectedId) || null;
  const notes: StartedRunNote[] = ledgerReady ? toolRunLedger.notes() : [];
  const pendingForSelected: PendingRun | undefined = ledgerReady && selected && !submitting
    ? toolRunLedger.pending(selected.id, canonicalInputKey(input))
    : undefined;
  const filtered = useMemo(() => {
    const needle = query.trim().toLowerCase();
    return tools.filter((tool) => !needle || `${tool.title} ${tool.id} ${tool.description}`.toLowerCase().includes(needle));
  }, [query, tools]);

  const selectTool = (id: string) => {
    if (id === selectedId) return;
    requestGeneration.current += 1;
    setSubmitting(false);
    setSelectedId(id);
    setInput({});
    setRawInput("{}");
    setJsonError(null);
    setMode("form");
    setRunBanner(null);
    setFieldErrors({});
    setActiveRun(null);
  };

  const runTool = async () => {
    if (!selected || submitting || (mode === "json" && jsonError)) return;
    const toolId = selected.id;
    const toolTitle = selected.title;
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
      const accepted = await startToolRun(toolId, input, runId);
      // Ledger updates happen even after an unmount: they are what a remounted page reads.
      toolRunLedger.forget(toolId, inputKey, runId);
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
    <div className="mx-auto max-w-7xl px-4 py-8">
      <div className="mb-6">
        <h1 className="text-2xl font-bold text-foreground">Tools</h1>
        <p className="mt-1 text-muted-foreground">Explore platform tools, fill their inputs and run them.</p>
      </div>
      {error ? <p role="alert" className="rounded-md border border-destructive/30 bg-destructive/10 p-3 text-sm text-destructive-text">{error}</p> : null}
      {notes.length > 0 ? (
        <ul data-testid="tool-runs-started-elsewhere" aria-label="Runs started for other tools" className="mb-4 space-y-2">
          {notes.map((item) => (
            <li key={item.runId} role="status" className="flex flex-wrap items-center gap-2 rounded-md border border-border bg-muted/40 p-3 text-sm text-foreground">
              <span>Run started for {item.toolTitle} <span className="font-mono text-xs text-muted-foreground">({item.toolId})</span>:</span>
              <Link href={`/runs/${item.runId}`} className="text-primary underline-offset-4 hover:underline">Open run</Link>
              <button type="button" aria-label={`Dismiss note for run ${item.runId}`} onClick={() => toolRunLedger.dismissNote(item.runId)} className="ml-auto text-xs text-muted-foreground hover:text-foreground">Dismiss</button>
            </li>
          ))}
        </ul>
      ) : null}
      {loading ? <p className="text-muted-foreground">Loading tools…</p> : null}
      {!loading && !error ? (
        <div className="grid gap-6 lg:grid-cols-[20rem_minmax(0,1fr)]">
          <aside className="space-y-3">
            <label htmlFor="tool-search" className="sr-only">Search tools</label>
            <input id="tool-search" type="search" placeholder="Search tools" value={query} onChange={(event) => setQuery(event.target.value)} className="w-full rounded-md border border-input bg-background px-3 py-2 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring" />
            <div className="space-y-2" aria-label="Tool catalog">
              {filtered.map((tool) => (
                <button key={tool.id} type="button" onClick={() => selectTool(tool.id)} className={`w-full rounded-md border p-3 text-left transition-colors ${selectedId === tool.id ? "border-primary bg-accent" : "border-border bg-card hover:bg-accent/60"}`}>
                  <span className="block font-medium text-card-foreground">{tool.title}</span>
                  <span className="block font-mono text-xs text-muted-foreground">{tool.id}</span>
                  <span className="mt-2 line-clamp-2 block text-sm text-muted-foreground">{tool.description}</span>
                  <span className="mt-2 flex flex-wrap gap-1">
                    {tool.side_effect ? <span className="rounded-full bg-destructive/10 px-2 py-0.5 text-xs text-destructive-text">Side effect</span> : <span className="rounded-full bg-muted px-2 py-0.5 text-xs text-muted-foreground">Read only</span>}
                    <span className={`rounded-full px-2 py-0.5 text-xs ${tool.allowed ? "bg-primary/10 text-primary" : "bg-destructive/10 text-destructive-text"}`}>{tool.allowed ? "Allowed" : "Disallowed"}</span>
                  </span>
                </button>
              ))}
              {filtered.length === 0 ? <p className="py-6 text-center text-sm text-muted-foreground">No tools match your search.</p> : null}
            </div>
          </aside>
          {selected ? (
            <main className="min-w-0 space-y-6">
              <section className="rounded-lg border border-border bg-card p-5">
                <h2 className="text-xl font-semibold text-card-foreground">{selected.title}</h2>
                <p className="font-mono text-xs text-muted-foreground">{selected.id}</p>
                <p className="mt-3 text-sm text-muted-foreground">{selected.description}</p>
              </section>
              <section className="rounded-lg border border-border bg-card p-5">
                <div className="mb-4 flex items-center justify-between gap-4">
                  <h3 className="font-semibold text-card-foreground">Input</h3>
                  <div role="group" aria-label="Input mode" className="flex rounded-md border border-border p-1">
                    {(["form", "json"] as const).map((item) => <button key={item} type="button" aria-pressed={mode === item} onClick={() => setMode(item)} className={`rounded px-3 py-1 text-sm ${mode === item ? "bg-primary text-primary-foreground" : "text-muted-foreground hover:bg-accent"}`}>{item === "form" ? "Form" : "JSON"}</button>)}
                  </div>
                </div>
                {mode === "form" ? <JsonSchemaForm schema={selected.input_schema} value={input} onChange={setFormInput} errors={fieldErrors} /> : (
                  <div className="space-y-1">
                    <label htmlFor="tool-input-json" className="sr-only">Tool input JSON</label>
                    <textarea id="tool-input-json" rows={12} value={rawInput} onChange={(event) => setJsonInput(event.target.value)} className="w-full rounded-md border border-input bg-background p-3 font-mono text-sm text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring" />
                    {jsonError ? <p role="alert" className="text-xs text-destructive-text">{jsonError}</p> : null}
                  </div>
                )}
                <div className="mt-5 space-y-3 border-t border-border pt-4">
                  {runBanner ? <p role="alert" data-testid="tool-run-banner" className="rounded-md border border-destructive/30 bg-destructive/10 p-3 text-sm text-destructive-text">{runBanner}</p> : null}
                  <div className="flex flex-wrap items-center gap-3">
                    <button type="button" onClick={runTool} disabled={submitting || (mode === "json" && Boolean(jsonError))} aria-busy={submitting} className="rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground disabled:cursor-not-allowed disabled:opacity-50">Run</button>
                    {submitting ? <span className="text-xs text-muted-foreground" aria-live="polite">Starting…</span> : null}
                    {pendingForSelected ? (
                      <span data-testid="tool-run-reuse-hint" data-state={pendingForSelected.state} className="text-xs text-muted-foreground">
                        {pendingForSelected.state === "not_started" ? "Running again sends the same request (same run id)." : UNCERTAIN_MESSAGE}
                      </span>
                    ) : null}
                    {!selected.allowed ? <span className="text-xs text-muted-foreground">This tool is not in the platform allowlist; the API will refuse the run.</span> : null}
                    {selected.allowed && selected.side_effect ? <span className="text-xs text-muted-foreground">This tool has side effects; each Run performs it once.</span> : null}
                  </div>
                </div>
              </section>
              {activeRun ? <ToolRunResult key={activeRun.runId} runId={activeRun.runId} initialStatus={activeRun.status} /> : null}
              <section className="rounded-lg border border-border bg-card p-5">
                <h3 className="mb-3 font-semibold text-card-foreground">Input schema</h3>
                <pre className="overflow-x-auto rounded-md bg-muted p-4 text-xs text-foreground">{JSON.stringify(selected.input_schema, null, 2)}</pre>
              </section>
            </main>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}
