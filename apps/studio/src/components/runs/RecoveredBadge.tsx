"use client";

import React from "react";
import { Badge } from "@/components/ui/Badge";
import type { Run } from "@/lib/runs";

/** The tooltip: how often DBOS resumed the run's workflow, and that finished steps were kept. */
export function recoveredTitle(attempts: number): string {
  return `Resumed by DBOS after ${attempts} restart${attempts === 1 ? "" : "s"}; completed steps were not re-run`;
}

/** "Recovered" (with "×N" from the second restart on) when DBOS carried the run across a worker crash or
 * a deploy (`recovered`, `recovery_attempts` of the run JSON); nothing otherwise. */
export function RecoveredBadge({ run }: { run: Pick<Run, "recovered" | "recovery_attempts"> | null | undefined }) {
  if (!run?.recovered) return null;
  const attempts = run.recovery_attempts ?? 1;
  return (
    <Badge variant="info" title={recoveredTitle(attempts)} data-testid="run-recovered-badge">
      Recovered{attempts > 1 ? ` ×${attempts}` : ""}
    </Badge>
  );
}
