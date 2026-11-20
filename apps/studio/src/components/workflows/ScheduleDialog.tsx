"use client";

import React, { useEffect, useState } from "react";
import { JsonSchemaForm } from "@/components/tools/JsonSchemaForm";
import { Button } from "@/components/ui/Button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/Dialog";
import { createSchedule, type WorkflowSchedule } from "@/lib/schedules";
import type { WorkflowSpec } from "@/lib/workflows";
import { cronPreview } from "./cronPreview";

export interface ScheduleDialogProps {
  workflowId: string;
  /** The version the schedule will run: its input_schema builds the Input form. */
  spec: WorkflowSpec | null;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onSaved: (schedule: WorkflowSchedule) => void;
}

const inputClass = "w-full rounded-md border border-input bg-background px-3 py-2 text-sm text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring";

function browserTimezone(): string {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
  } catch {
    return "UTC";
  }
}

/** New schedule: Name, Cron (with a live reading), Timezone and the run Input. The API validates all of it. */
export function ScheduleDialog({ workflowId, spec, open, onOpenChange, onSaved }: ScheduleDialogProps) {
  const [name, setName] = useState("");
  const [cron, setCron] = useState("0 * * * *");
  const [timezone, setTimezone] = useState(browserTimezone);
  const [input, setInput] = useState<Record<string, unknown>>({});
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!open) return;
    setName("");
    setCron("0 * * * *");
    setTimezone(browserTimezone());
    setInput({});
    setError(null);
  }, [open]);

  const save = async (event: React.FormEvent) => {
    event.preventDefault();
    if (busy) return;
    setBusy(true);
    setError(null);
    try {
      const saved = await createSchedule(workflowId, { name: name.trim(), cron: cron.trim(), timezone: timezone.trim(), input });
      onSaved(saved);
      onOpenChange(false);
    } catch (reason: unknown) {
      setError(reason instanceof Error ? reason.message : "Failed to create the schedule");
    } finally {
      setBusy(false);
    }
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>New schedule</DialogTitle>
          <DialogDescription>Runs the latest published version on a cron.</DialogDescription>
        </DialogHeader>
        <form onSubmit={save} className="flex flex-col gap-3">
          <div className="space-y-1">
            <label htmlFor="schedule-name" className="text-label font-medium text-foreground">Name</label>
            <input id="schedule-name" value={name} onChange={(e) => setName(e.target.value)} maxLength={100} required className={inputClass} />
          </div>
          <div className="space-y-1">
            <label htmlFor="schedule-cron" className="text-label font-medium text-foreground">Cron</label>
            <input id="schedule-cron" value={cron} onChange={(e) => setCron(e.target.value)} required spellCheck={false} aria-describedby="schedule-cron-preview" className={`${inputClass} font-mono`} />
            <p id="schedule-cron-preview" data-testid="schedule-cron-preview" className="text-caption text-muted-foreground">{cronPreview(cron)}</p>
          </div>
          <div className="space-y-1">
            <label htmlFor="schedule-timezone" className="text-label font-medium text-foreground">Timezone</label>
            <input id="schedule-timezone" value={timezone} onChange={(e) => setTimezone(e.target.value)} required spellCheck={false} className={inputClass} />
          </div>
          {spec && Object.keys(spec.input_schema?.properties ?? {}).length > 0 ? (
            <JsonSchemaForm schema={spec.input_schema} value={input} onChange={setInput} idPrefix="schedule-input" />
          ) : null}
          {error ? <p data-testid="schedule-input-error" role="alert" className="rounded-md border border-destructive/30 bg-destructive/10 p-2 text-label text-destructive-text">{error}</p> : null}
          <DialogFooter>
            <Button variant="secondary" onClick={() => onOpenChange(false)}>Cancel</Button>
            <Button type="submit" disabled={busy || !name.trim() || !cron.trim()}>{busy ? "Creating…" : "Create schedule"}</Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
