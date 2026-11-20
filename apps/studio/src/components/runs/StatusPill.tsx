"use client";

import React from "react";
import type { RunStatus } from "@/lib/runs";

interface StatusPillProps {
  status: RunStatus;
  showRecoveringNotice?: boolean;
}

const statusStyles: Record<RunStatus, string> = {
  queued: "bg-muted text-card-foreground",
  running: "bg-accent text-accent-foreground",
  recovering: "bg-warning/10 text-warning-text",
  // AC.14: a running turn that waits for a person to approve a tool call.
  waiting_approval: "bg-warning/15 text-warning-text ring-1 ring-inset ring-warning",
  completed: "bg-success/10 text-success-text",
  // A "nothing to do" exit (workflow.return outcome "skipped"): a success, shown quietly in grey.
  skipped: "bg-muted text-muted-foreground",
  failed: "bg-destructive/10 text-destructive-text",
  needs_attention: "bg-warning/15 text-warning-text ring-1 ring-inset ring-warning",
};

const LABELS: Partial<Record<RunStatus, string>> = { waiting_approval: "Waiting for approval" };

export function statusLabel(status: RunStatus): string {
  return LABELS[status] ?? status.replace("_", " ").replace(/\b\w/g, (c) => c.toUpperCase());
}

export function StatusPill({ status, showRecoveringNotice = false }: StatusPillProps) {
  return (
    <div className="flex items-center gap-2">
      <span data-testid="status-pill" data-status={status} className={`inline-flex items-center px-3 py-1 rounded-full text-sm font-medium ${statusStyles[status] ?? statusStyles.running}`}>
        {statusLabel(status)}
      </span>
      {showRecoveringNotice && status === "recovering" && (
        <span className="text-xs text-warning-text">
          Worker is restarting, events may be delayed
        </span>
      )}
    </div>
  );
}
