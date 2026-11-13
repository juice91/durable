"use client";

import React from "react";

type Status = "queued" | "running" | "recovering" | "completed" | "failed" | "needs_attention";

interface StatusPillProps {
  status: Status;
  showRecoveringNotice?: boolean;
}

const statusStyles: Record<Status, string> = {
  queued: "bg-muted text-card-foreground",
  running: "bg-accent text-accent-foreground",
  recovering: "bg-warning/10 text-warning-text",
  completed: "bg-success/10 text-success-text",
  failed: "bg-destructive/10 text-destructive-text",
  needs_attention: "bg-warning/15 text-warning-text ring-1 ring-inset ring-warning",
};

export function StatusPill({ status, showRecoveringNotice = false }: StatusPillProps) {
  const displayText = status.replace("_", " ").replace(/\b\w/g, (c) => c.toUpperCase());

  return (
    <div className="flex items-center gap-2">
      <span className={`inline-flex items-center px-3 py-1 rounded-full text-sm font-medium ${statusStyles[status]}`}>
        {displayText}
      </span>
      {showRecoveringNotice && status === "recovering" && (
        <span className="text-xs text-warning-text">
          Worker is restarting, events may be delayed
        </span>
      )}
    </div>
  );
}
