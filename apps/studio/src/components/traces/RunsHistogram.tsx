"use client";

import React, { useEffect, useState } from "react";
import { fetchRunsHistogram, type RunsHistogram as Histogram, type RunsHistogramBucket, type TraceFilters } from "@/lib/traces";

const HOUR_MS = 3_600_000;
const DAY_MS = 24 * HOUR_MS;
const WIDTH = 600;
const HEIGHT = 72;
const GAP = 2;

export interface RunsHistogramProps {
  /** The list filters (the histogram counts the runs they match). */
  filters: TraceFilters;
  /** A bucket was picked: narrow the list's dates to it. */
  onPickBucket: (range: { from: string; to: string }) => void;
}

function bucketLabel(bucket: RunsHistogramBucket, interval: Histogram["interval"]): string {
  const at = new Date(bucket.start);
  const when = interval === "hour" ? at.toLocaleString() : at.toLocaleDateString();
  const failed = bucket.failed > 0 ? `, ${bucket.failed} failed` : "";
  return `${when}: ${bucket.runs} ${bucket.runs === 1 ? "run" : "runs"}${failed}`;
}

/**
 * A bar strip of the runs the filters match: one bar per bucket (hour or day, from the API), a
 * failed segment on the bar, and a click (or Enter) on a bar narrows the dates to that bucket.
 * Plain SVG in theme tokens, no chart library.
 */
export function RunsHistogram({ filters, onPickBucket }: RunsHistogramProps) {
  const [histogram, setHistogram] = useState<Histogram | null>(null);
  const [error, setError] = useState(false);

  useEffect(() => {
    const controller = new AbortController();
    setError(false);
    fetchRunsHistogram(filters, controller.signal)
      .then((data) => {
        if (!controller.signal.aborted) setHistogram(data);
      })
      .catch(() => {
        if (!controller.signal.aborted) {
          setHistogram(null);
          setError(true);
        }
      });
    return () => controller.abort();
    // The request depends on the filters' content, not their identity.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [JSON.stringify(filters)]);

  if (error) return <p role="status" className="text-caption text-muted-foreground">Could not load the runs histogram.</p>;
  if (!histogram || !Array.isArray(histogram.buckets) || histogram.buckets.length === 0) return null;

  const { buckets, interval } = histogram;
  const span = interval === "hour" ? HOUR_MS : DAY_MS;
  const max = Math.max(1, ...buckets.map((b) => b.runs));
  const slot = WIDTH / buckets.length;
  const barWidth = Math.max(1, slot - GAP);

  const pick = (bucket: RunsHistogramBucket) => {
    const from = new Date(bucket.start).getTime();
    onPickBucket({ from: new Date(from).toISOString(), to: new Date(from + span).toISOString() });
  };

  return (
    <div data-testid="runs-histogram" className="min-w-0 rounded-lg border border-border bg-surface p-3">
      <p className="mb-1 text-caption text-muted-foreground">Runs per {interval}. Pick a bar to narrow the dates.</p>
      <svg viewBox={`0 0 ${WIDTH} ${HEIGHT}`} preserveAspectRatio="none" className="h-20 w-full text-primary" aria-label="Runs over time">
        {buckets.map((bucket, i) => {
          const total = (bucket.runs / max) * (HEIGHT - 2);
          const failed = Math.min(total, (bucket.failed / max) * (HEIGHT - 2));
          const x = i * slot;
          const label = bucketLabel(bucket, interval);
          return (
            <g
              key={bucket.start}
              role="button"
              tabIndex={0}
              aria-label={label}
              className="cursor-pointer outline-none [&:focus-visible>rect:nth-of-type(1)]:stroke-ring"
              onClick={() => pick(bucket)}
              onKeyDown={(event) => {
                if (event.key === "Enter" || event.key === " ") {
                  event.preventDefault();
                  pick(bucket);
                }
              }}
            >
              <title>{label}</title>
              {/* The hit area covers the whole column, so an empty bucket can be picked too. */}
              <rect x={x} y={0} width={slot} height={HEIGHT} className="fill-transparent stroke-transparent" strokeWidth={2} />
              <rect x={x + GAP / 2} y={HEIGHT - total} width={barWidth} height={total} className="fill-current" />
              {failed > 0 ? (
                <rect data-testid="histogram-failed" x={x + GAP / 2} y={HEIGHT - failed} width={barWidth} height={failed} className="fill-destructive" />
              ) : null}
            </g>
          );
        })}
      </svg>
    </div>
  );
}
