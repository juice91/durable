"use client";

import { CopyButton } from "@/components/ui/CopyButton";
import React, { useCallback, useEffect, useRef, useState } from "react";
import { DataList, type DataListColumn } from "@/components/shared/DataList";
import { ConfirmDialog } from "@/components/shared/ConfirmDialog";
import { Button } from "@/components/ui/Button";
import { formatDateTime } from "@/components/approvals/ApprovalCard";
import { ConflictNotice } from "@/components/shared/ConflictNotice";
import { useConflict } from "@/lib/useConflict";
import { deleteSchedule, lastRunText, listSchedules, scheduleState, updateSchedule, type WorkflowSchedule } from "@/lib/schedules";
import type { WorkflowSpec } from "@/lib/workflows";
import { ScheduleDialog } from "./ScheduleDialog";

export interface WorkflowSchedulesProps {
  /** One workflow's schedules (its Schedules tab); omitted on the global page. */
  workflowId?: string;
  /** The workflow's name, for the empty text. */
  target?: string;
  /** The published spec the New schedule dialog builds its input form from (needs `workflowId`). */
  spec?: WorkflowSpec | null;
  /** Called with the schedule count after every load or change (the tab badge). */
  onCountChange?: (count: number) => void;
}

/**
 * The Schedules table: Target, Schedule ID, Cron, Next run, State, and Pause / Resume / Delete per
 * row. A schedule the worker stopped shows "Stopped: <reason>"; Resume patches active=true, which
 * the API re-validates (and clears the reason).
 */
export function WorkflowSchedules({ workflowId, target, spec = null, onCountChange }: WorkflowSchedulesProps) {
  const [rows, setRows] = useState<WorkflowSchedule[] | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const [deleting, setDeleting] = useState<WorkflowSchedule | null>(null);
  const [deleteError, setDeleteError] = useState<string | null>(null);
  const countRef = useRef(onCountChange);
  countRef.current = onCountChange;

  const apply = useCallback((next: WorkflowSchedule[]) => {
    setRows(next);
    countRef.current?.(next.length);
  }, []);

  const load = useCallback(async (signal?: AbortSignal) => {
    setLoadError(null);
    try {
      const all: WorkflowSchedule[] = [];
      let before: string | undefined;
      do {
        const page = await listSchedules({ workflowId, before, signal });
        all.push(...page.items);
        before = page.next_before ?? undefined;
      } while (before && !signal?.aborted);
      if (!signal?.aborted) apply(all);
    } catch (reason) {
      if (!signal?.aborted) setLoadError(reason instanceof Error ? reason.message : "Failed to load schedules");
    }
  }, [workflowId, apply]);

  useEffect(() => {
    const controller = new AbortController();
    setRows(null);
    void load(controller.signal);
    return () => controller.abort();
  }, [load]);

  // 412: someone else changed the schedule; 重新載入 shows their version, 覆寫 resends on top of it.
  const conflict = useConflict<WorkflowSchedule>({ onSaved: (saved) => replaceRow(saved as WorkflowSchedule) });
  const replaceRow = (saved: WorkflowSchedule) => apply((rows ?? []).map((r) => (r.id === saved.id ? saved : r)));

  const setActive = async (row: WorkflowSchedule, active: boolean) => {
    setBusyId(row.id);
    setActionError(null);
    try {
      const saved = await conflict.guard(() => updateSchedule(row.id, { active }));
      if (saved !== undefined) replaceRow(saved);
    } catch (reason) {
      setActionError(`${row.name}: ${reason instanceof Error ? reason.message : "Failed to update the schedule"}`);
    } finally {
      setBusyId(null);
    }
  };

  const confirmDelete = async () => {
    if (!deleting) return;
    setBusyId(deleting.id);
    setDeleteError(null);
    try {
      await deleteSchedule(deleting.id);
      apply((rows ?? []).filter((r) => r.id !== deleting.id));
      setDeleting(null);
    } catch (reason) {
      setDeleteError(reason instanceof Error ? reason.message : "Failed to delete the schedule");
    } finally {
      setBusyId(null);
    }
  };

  const columns: DataListColumn<WorkflowSchedule>[] = [
    {
      id: "target", header: "Target", sortable: true,
      sortValue: (row) => (row.workflow_name ?? row.workflow_id).toLowerCase(),
      cell: (row) => (
        <span className="flex min-w-0 flex-col">
          <span>{row.workflow_name ?? row.workflow_id}</span>
          <span className="text-caption text-muted-foreground">{row.name}</span>
        </span>
      ),
    },
    { id: "id", header: "Schedule ID", cell: (row) => <span className="flex min-w-0 items-center gap-1"><span className="select-text font-mono text-label">{row.id}</span><CopyButton text={row.id} label="schedule id" /></span> },
    { id: "cron", header: "Cron", cell: (row) => <span className="flex min-w-0 items-center gap-1"><span className="select-text font-mono text-label">{row.cron}</span><CopyButton text={row.cron} label="cron" /></span> },
    { id: "next", header: "Next run", sortable: true, sortValue: (row) => row.next_fire_at ?? "", cell: (row) => (row.active ? formatDateTime(row.next_fire_at) : "—") },
    {
      id: "state", header: "State",
      cell: (row) => {
        const text = scheduleState(row);
        const lastRun = lastRunText(row);
        return (
          <span className="flex min-w-0 flex-col">
            <span data-state={row.active ? "active" : row.last_error ? "stopped" : "paused"} className={row.last_error && !row.active ? "text-destructive-text" : undefined}>{text}</span>
            {lastRun ? <span data-testid={`schedule-last-run-${row.id}`} data-status={row.last_run_status} className="text-caption text-muted-foreground">{lastRun}</span> : null}
          </span>
        );
      },
    },
    {
      id: "actions", header: <span className="sr-only">Actions</span>, align: "end",
      cell: (row) => (
        <span className="flex justify-end gap-1">
          {row.active ? (
            <Button size="sm" disabled={busyId === row.id} aria-label={`Pause ${row.name}`} onClick={() => void setActive(row, false)}>Pause</Button>
          ) : (
            <Button size="sm" disabled={busyId === row.id} aria-label={`Resume ${row.name}`} onClick={() => void setActive(row, true)}>Resume</Button>
          )}
          <Button size="sm" variant="ghost" disabled={busyId === row.id} aria-label={`Delete ${row.name}`} onClick={() => { setDeleteError(null); setDeleting(row); }}>Delete</Button>
        </span>
      ),
    },
  ];

  return (
    <div className="flex min-w-0 flex-col gap-3">
      <ConflictNotice state={conflict} onReload={(current) => (current ? replaceRow(current) : void load())} />
      {actionError ? <p role="alert" className="rounded-md border border-destructive/30 bg-destructive/10 p-2 text-label text-destructive-text">{actionError}</p> : null}
      <DataList<WorkflowSchedule>
        label="Schedules"
        rows={rows ?? []}
        columns={columns}
        getRowId={(row) => row.id}
        searchPlaceholder="Filter by id or workflow"
        filterRow={(row, query) => {
          const q = query.toLowerCase();
          return row.id.toLowerCase().includes(q) || row.name.toLowerCase().includes(q) || (row.workflow_name ?? "").toLowerCase().includes(q);
        }}
        toolbar={workflowId ? <Button variant="primary" onClick={() => setCreating(true)}>New schedule</Button> : undefined}
        loading={rows === null && !loadError}
        error={loadError}
        onRetry={() => void load()}
        empty={{
          title: "No schedules",
          description: target
            ? `${target} has no cron schedule. A schedule starts a run on its own.`
            : "No workflow has a cron schedule. Create one from a workflow's Schedules tab.",
        }}
        testId="workflow-schedules"
      />
      {workflowId ? (
        <ScheduleDialog
          workflowId={workflowId}
          spec={spec}
          open={creating}
          onOpenChange={setCreating}
          onSaved={(saved) => apply([saved, ...(rows ?? [])])}
        />
      ) : null}
      {deleting ? (
        <ConfirmDialog
          title="Delete this schedule?"
          description={`${deleting.name} will stop firing. Runs it already started are kept.`}
          confirmLabel="Delete schedule"
          destructive
          busy={busyId === deleting.id}
          error={deleteError}
          onConfirm={() => void confirmDelete()}
          onCancel={() => setDeleting(null)}
        />
      ) : null}
    </div>
  );
}
