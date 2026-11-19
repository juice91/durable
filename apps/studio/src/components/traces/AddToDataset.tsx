"use client";

import React, { useEffect, useId, useState } from "react";
import { Button } from "@/components/ui/Button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle, DialogTrigger } from "@/components/ui/Dialog";
import { toast } from "@/components/ui/Toaster";
import { addCase } from "@/lib/evals";
import { listAllSuites, type OverviewSuite } from "@/lib/evalOverview";
import type { TraceRun } from "@/lib/traces";

const field =
  "rounded-md border border-input bg-surface px-2 text-label text-foreground placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring";

/** Expected output as the editable text: a JSON string stays text, other JSON is shown as written. */
function outputText(run: TraceRun): string {
  return typeof run.output === "string" ? run.output : "";
}

/**
 * "Add to test suite": pick one of the tenant's test suites and create a case from the trace's input,
 * with the trace's output as the expected output (editable before saving).
 */
export function AddToDataset({ run }: { run: TraceRun }) {
  const id = useId();
  const [open, setOpen] = useState(false);
  const [suites, setSuites] = useState<OverviewSuite[] | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [suiteId, setSuiteId] = useState("");
  const [name, setName] = useState("");
  const [expected, setExpected] = useState("");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!open) return;
    setName(`trace ${run.run_id.slice(0, 8)}`);
    setExpected(outputText(run));
    setError(null);
    setSuiteId("");
    setSuites(null);
    setLoadError(null);
    const controller = new AbortController();
    listAllSuites({ limit: 100, signal: controller.signal })
      .then((page) => setSuites(page.items))
      .catch((err) => {
        if (controller.signal.aborted) return;
        setSuites([]);
        setLoadError(err instanceof Error && err.message ? err.message : "Could not load the test suites");
      });
    return () => controller.abort();
  }, [open, run]);

  const save = async (event: React.FormEvent) => {
    event.preventDefault();
    if (!suiteId) {
      setError("Choose a test suite.");
      return;
    }
    if (!name.trim()) {
      setError("Give the case a name.");
      return;
    }
    setSaving(true);
    setError(null);
    try {
      await addCase(suiteId, {
        name: name.trim(),
        input: run.input ?? "",
        expected_output: expected.trim() === "" ? null : expected,
        metadata: { source_trace_id: run.run_id },
        evaluators: [],
      });
      const suite = suites?.find((s) => s.id === suiteId);
      toast(`Added to ${suite?.name ?? "the test suite"}`);
      setOpen(false);
    } catch (err) {
      setError(err instanceof Error && err.message ? err.message : "Could not add the case");
    } finally {
      setSaving(false);
    }
  };

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger asChild>
        <Button variant="secondary" size="sm" data-testid="add-to-dataset">Add to test suite</Button>
      </DialogTrigger>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Add to test suite</DialogTitle>
          <DialogDescription>Creates a test case from this trace&apos;s input. The expected output starts as the trace&apos;s output; edit it before saving.</DialogDescription>
        </DialogHeader>
        <form onSubmit={save} className="flex min-w-0 flex-col gap-3">
          <label htmlFor={`${id}-suite`} className="text-caption font-medium text-muted-foreground">Test suite</label>
          <select id={`${id}-suite`} value={suiteId} onChange={(e) => setSuiteId(e.target.value)} className={`${field} h-8`} disabled={suites === null}>
            <option value="">{suites === null ? "Loading..." : suites.length === 0 ? "No test suites" : "Choose a test suite"}</option>
            {(suites ?? []).map((s) => (
              <option key={s.id} value={s.id}>{s.agent_name ? `${s.agent_name} / ${s.name}` : s.name}</option>
            ))}
          </select>
          {loadError ? <p role="alert" className="text-label text-destructive-text">{loadError}</p> : null}
          <label htmlFor={`${id}-name`} className="text-caption font-medium text-muted-foreground">Case name</label>
          <input id={`${id}-name`} value={name} maxLength={100} onChange={(e) => setName(e.target.value)} className={`${field} h-8`} />
          <label htmlFor={`${id}-input`} className="text-caption font-medium text-muted-foreground">Input (from the trace)</label>
          <textarea id={`${id}-input`} value={run.input ?? ""} readOnly rows={3} className={`${field} w-full py-1 font-mono`} />
          <label htmlFor={`${id}-expected`} className="text-caption font-medium text-muted-foreground">Expected output</label>
          <textarea id={`${id}-expected`} value={expected} rows={3} onChange={(e) => setExpected(e.target.value)} className={`${field} w-full py-1 font-mono`} />
          {error ? <p role="alert" className="text-label text-destructive-text">{error}</p> : null}
          <DialogFooter>
            <Button type="submit" variant="primary" disabled={saving}>{saving ? "Adding..." : "Add case"}</Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
