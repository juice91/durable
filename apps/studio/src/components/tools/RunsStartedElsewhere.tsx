"use client";

import React from "react";
import Link from "next/link";
import { routes } from "@/lib/routes";
import { toolRunLedger } from "@/lib/toolRunLedger";
import { useToolRunLedger } from "./ToolRunner";

/**
 * "Run started for X: Open run" notes: runs whose 202 arrived after their runner was gone (the Sheet
 * closed or switched tools). Kept in `toolRunLedger` (sessionStorage) until dismissed.
 */
export function RunsStartedElsewhere({ className }: { className?: string }) {
  const ready = useToolRunLedger();
  const notes = ready ? toolRunLedger.notes() : [];
  if (notes.length === 0) return null;
  return (
    <ul data-testid="tool-runs-started-elsewhere" aria-label="Runs started for other tools" className={className ?? "space-y-2"}>
      {notes.map((item) => (
        <li key={item.runId} role="status" className="flex min-w-0 flex-wrap items-center gap-2 rounded-md border border-border bg-muted/40 p-3 text-body text-foreground">
          <span className="min-w-0">Run started for {item.toolTitle} <span className="font-mono text-caption text-muted-foreground">({item.toolId})</span>:</span>
          <Link href={routes.run(item.runId)} className="text-foreground underline underline-offset-4 hover:no-underline">Open run</Link>
          <button type="button" aria-label={`Dismiss note for run ${item.runId}`} onClick={() => toolRunLedger.dismissNote(item.runId)} className="ml-auto rounded text-caption text-muted-foreground hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring">Dismiss</button>
        </li>
      ))}
    </ul>
  );
}
