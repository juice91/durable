"use client";

import React from "react";
import Link from "next/link";
import type { PeriodDelta } from "@/lib/metrics";

export interface KpiCardProps {
  label: string;
  value: string;
  delta?: PeriodDelta | null;
  /** E.g. "vs previous period"; shown after the delta text. */
  deltaLabel?: string;
  /** Secondary line under the value (breakdown, unpriced note). */
  hint?: React.ReactNode;
  /** Drill-down: the whole card links here. */
  href?: string | null;
  loading?: boolean;
  testId?: string;
}

const ARROWS = { up: "▲", down: "▼", flat: "–" } as const;

export function KpiCard({ label, value, delta, deltaLabel = "vs previous period", hint, href, loading = false, testId }: KpiCardProps) {
  const body = (
    <>
      <div className="text-xs font-medium text-muted-foreground">{label}</div>
      {loading ? (
        <div className="mt-2 h-7 w-24 rounded bg-muted animate-pulse" aria-label={`Loading ${label}`} />
      ) : (
        <div className="mt-1 text-2xl font-semibold text-foreground" data-testid={testId ? `${testId}-value` : undefined}>
          {value}
        </div>
      )}
      {!loading && delta && (
        <div className="mt-1 text-xs text-muted-foreground" data-tone={delta.tone} data-testid={testId ? `${testId}-delta` : undefined}>
          <span aria-hidden="true" className={delta.tone === "worse" ? "font-semibold text-foreground" : undefined}>
            {ARROWS[delta.direction]}{" "}
          </span>
          <span>{delta.text}</span>
          {delta.tone !== "neutral" && <span className="sr-only"> ({delta.tone})</span>}
          {deltaLabel && !delta.noPrevious && <span> {deltaLabel}</span>}
        </div>
      )}
      {!loading && hint && <div className="mt-1 text-xs text-muted-foreground">{hint}</div>}
    </>
  );
  const box = "block rounded-lg border border-border bg-card text-card-foreground p-4 min-w-0";
  return href && !loading ? (
    <Link href={href} className={`${box} hover:bg-muted/50 transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring`} data-testid={testId}>
      {body}
    </Link>
  ) : (
    <div className={box} data-testid={testId}>{body}</div>
  );
}
