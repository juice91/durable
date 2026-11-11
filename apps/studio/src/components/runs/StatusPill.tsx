"use client";

import React from "react";

type Status = "queued" | "running" | "recovering" | "completed" | "failed" | "needs_attention";

interface StatusPillProps {
  status: Status;
  showRecoveringNotice?: boolean;
}

const statusStyles: Record<Status, string> = {
  queued: "bg-gray-100 text-gray-800",
  running: "bg-blue-100 text-blue-800",
  recovering: "bg-yellow-100 text-yellow-800",
  completed: "bg-green-100 text-green-800",
  failed: "bg-red-100 text-red-800",
  needs_attention: "bg-orange-100 text-orange-800",
};

export function StatusPill({ status, showRecoveringNotice = false }: StatusPillProps) {
  const displayText = status.replace("_", " ").replace(/\b\w/g, (c) => c.toUpperCase());

  return (
    <div className="flex items-center gap-2">
      <span className={`inline-flex items-center px-3 py-1 rounded-full text-sm font-medium ${statusStyles[status]}`}>
        {displayText}
      </span>
      {showRecoveringNotice && status === "recovering" && (
        <span className="text-xs text-yellow-600">
          Worker is restarting, events may be delayed
        </span>
      )}
    </div>
  );
}
