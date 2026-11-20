"use client";

import React from "react";
import { Badge } from "@/components/ui/Badge";
import type { Run } from "@/lib/runs";

/** The tooltip: how often DBOS resumed the run's workflow, and that finished steps were kept. */
export function recoveredTitle(restarts: number): string {
  return `Resumed by DBOS after ${restarts} restart${restarts === 1 ? "" : "s"}; completed steps were not re-run`;
}

/** "Recovered" (with "×N" from the second restart on) when DBOS carried the run across a worker crash or
 * a deploy (`recovered`); `restarts` is `recovery_attempts - 1` (DBOS counts the first start). */
export function RecoveredBadge({ run }: { run: Pick<Run, "recovered" | "recovery_attempts"> | null | undefined }) {
  if (!run?.recovered) return null;
  const restarts = (run.recovery_attempts ?? 1) - 1;
  const count = restarts > 0 ? restarts : 1;
  return (
    <Badge variant="info" title={recoveredTitle(restarts)} data-testid="run-recovered-badge">
      Recovered{restarts > 1 ? ` ×${restarts}` : ""}
    </Badge>
  );
}
