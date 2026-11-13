"use client";

import React, { useEffect, useId, useMemo, useState } from "react";
import "./chartTheme.css";
import { formatBucket, niceMax, type MetricsInterval } from "@/lib/metrics";

export interface ChartSeries {
  key: string;
  label: string;
  /** A CSS colour, normally a palette variable: `var(--viz-series-1-color)`, `var(--viz-critical-color)`. */
  color: string;
  values: readonly (number | null)[];
  /** Bar charts: a drill-down link for this series' bar in bucket `index` (e.g. failed runs only). */
  href?: (index: number) => string | null;
}

export interface TimeSeriesChartProps {
  title: string;
  description?: string;
  buckets: readonly { start: string }[];
  interval: MetricsInterval;
  series: readonly ChartSeries[];
  type: "bar" | "line";
  formatValue: (value: number) => string;
  /** Drill-down for the whole bucket (used when a series has no own `href`, and for line charts). */
  bucketHref?: (index: number) => string | null;
  /** Client-side navigation for drill-down clicks; without it the links navigate normally. */
  onNavigate?: (href: string) => void;
  /** One-sentence text summary (read by screen readers, shown under the chart). */
  summary?: string;
  /** Extra tooltip lines for bucket `index` (e.g. unpriced requests). */
  extraTooltip?: (index: number) => string[];
  /** What a drill-down opens, e.g. "Open runs created in this window" (default "Open matching runs"). */
  drillLabel?: string;
  loading?: boolean;
  error?: string | null;
  emptyMessage?: string;
  /** Whole-number values (counts): no fractional axis tick. */
  integer?: boolean;
  className?: string;
}

const W = 640;
const H = 220;
const M = { top: 12, right: 12, bottom: 26, left: 60 };
const PLOT_W = W - M.left - M.right;
const PLOT_H = H - M.top - M.bottom;
const GAP = 2; // surface gap between adjacent bars
const MARKER_LIMIT = 48; // draw point markers only when buckets are wide enough

/** A bar anchored to the baseline with rounded top corners (radius capped by the bar's size). */
export function barPath(x: number, y: number, w: number, h: number, r = 4): string {
  if (h <= 0 || w <= 0) return "";
  const rr = Math.max(0, Math.min(r, w / 2, h));
  const base = y + h;
  return [
    `M${x},${base}`,
    `V${y + rr}`,
    `Q${x},${y} ${x + rr},${y}`,
    `H${x + w - rr}`,
    `Q${x + w},${y} ${x + w},${y + rr}`,
    `V${base}`,
    "Z",
  ].join(" ");
}

/** Geometry shared by the renderer and the tests. */
export function chartGeometry(bucketCount: number, seriesCount: number, max: number) {
  const n = Math.max(1, bucketCount);
  const slot = PLOT_W / n;
  const group = Math.max(1, slot * 0.72);
  const bar = Math.max(0.5, (group - GAP * (seriesCount - 1)) / Math.max(1, seriesCount));
  const yMax = niceMax(max);
  const y = (v: number) => M.top + PLOT_H - (Math.max(0, v) / yMax) * PLOT_H;
  const xCenter = (i: number) => M.left + slot * (i + 0.5);
  return { slot, group, bar, yMax, y, xCenter };
}

/** Line path segments, broken at null values. */
export function linePaths(values: readonly (number | null)[], x: (i: number) => number, y: (v: number) => number): string[] {
  const paths: string[] = [];
  let current = "";
  values.forEach((v, i) => {
    if (v === null) {
      if (current) paths.push(current);
      current = "";
      return;
    }
    current += `${current ? "L" : "M"}${x(i).toFixed(2)},${y(v).toFixed(2)}`;
  });
  if (current) paths.push(current);
  return paths;
}

function navigateHandler(href: string, onNavigate?: (href: string) => void) {
  return (event: React.MouseEvent) => {
    if (!onNavigate || event.metaKey || event.ctrlKey || event.shiftKey || event.button !== 0) return;
    event.preventDefault();
    onNavigate(href);
  };
}

const DEFAULT_DRILL_LABEL = "Open matching runs";

/** Whether focus came from the keyboard (`:focus-visible`); false where the selector is unsupported. */
function isFocusVisible(el: Element): boolean {
  try {
    return el.matches(":focus-visible");
  } catch {
    return false;
  }
}

function valueText(v: number | null | undefined, formatValue: (value: number) => string): string {
  return v === null || v === undefined ? "no data" : formatValue(v);
}

export function TimeSeriesChart({
  title,
  description,
  buckets,
  interval,
  series,
  type,
  formatValue,
  bucketHref,
  onNavigate,
  summary,
  extraTooltip,
  drillLabel = DEFAULT_DRILL_LABEL,
  loading = false,
  error = null,
  emptyMessage = "No data in this period",
  integer = false,
  className = "",
}: TimeSeriesChartProps) {
  const titleId = useId();
  const summaryId = useId();
  const [activeState, setActive] = useState<number | null>(null);
  const [activeSeries, setActiveSeries] = useState(0);
  const [keyboard, setKeyboard] = useState(false);

  const max = useMemo(
    () => Math.max(0, ...series.flatMap((s) => s.values.map((v) => v ?? 0))),
    [series],
  );
  const empty = buckets.length === 0 || series.every((s) => s.values.every((v) => v === null || v === 0));
  // The selected bucket, clamped to the current data: a refresh may return fewer buckets.
  const active = activeState !== null && activeState < buckets.length ? activeState : null;
  const plotShown = !error && !empty && !(loading && buckets.length === 0);
  const bucketsKey = `${buckets.length}|${buckets[0]?.start ?? ""}|${buckets[buckets.length - 1]?.start ?? ""}`;
  // New buckets, or the plot going away (error, empty, loading) without a blur: drop the selection
  // and the keyboard mode so the live region and the tooltip never describe stale data.
  useEffect(() => {
    setActive(null);
    setKeyboard(false);
  }, [bucketsKey, plotShown]);
  const geo = chartGeometry(buckets.length, type === "bar" ? series.length : 1, max);
  const ticks = integer && !Number.isInteger(geo.yMax / 2) ? [0, geo.yMax] : [0, geo.yMax / 2, geo.yMax];
  const xLabelIdx =
    buckets.length >= 3 ? [0, Math.floor((buckets.length - 1) / 2), buckets.length - 1] : buckets.map((_, i) => i);
  // Bar charts whose series carry their own links (runs vs failed) drill down per series.
  const perSeries = type === "bar" && series.some((s) => s.href);
  const seriesIdx = Math.min(activeSeries, Math.max(0, series.length - 1));

  const tooltipLines = (i: number) => [
    ...series.map((s) => `${s.label}: ${valueText(s.values[i], formatValue)}`),
    ...(extraTooltip?.(i) ?? []),
  ];
  const bucketLabel = (i: number) => formatBucket(buckets[i].start, interval);
  const hrefFor = (i: number, k: number | null): string | null => {
    if (perSeries && k !== null) return (series[k]?.href ?? bucketHref)?.(i) ?? null;
    return bucketHref?.(i) ?? null;
  };
  const open = (href: string) => {
    if (onNavigate) onNavigate(href);
    else window.location.assign(href);
  };

  // The live readout of the keyboard-selected bucket (and series, for per-series charts).
  const liveText =
    active === null || !buckets[active]
      ? ""
      : perSeries
        ? `${bucketLabel(active)}, ${series[seriesIdx].label}: ${valueText(series[seriesIdx].values[active], formatValue)}. ${tooltipLines(active).join(", ")}.${hrefFor(active, seriesIdx) ? ` Press Enter to ${drillLabel.toLowerCase()}.` : ""}`
        : `${bucketLabel(active)}: ${tooltipLines(active).join(", ")}.${hrefFor(active, null) ? ` Press Enter to ${drillLabel.toLowerCase()}.` : ""}`;

  // Functional updates so keys pressed faster than a re-render never read a stale bucket.
  const onKeyDown = (event: React.KeyboardEvent) => {
    if (buckets.length === 0) return;
    const last = buckets.length - 1;
    const move: Record<string, (cur: number) => number> = {
      ArrowRight: (cur) => Math.min(last, cur + 1),
      ArrowLeft: (cur) => Math.max(0, cur - 1),
      Home: () => 0,
      End: () => last,
    };
    if (move[event.key]) {
      event.preventDefault();
      setKeyboard(true);
      setActive((a) => move[event.key](Math.min(a ?? last, last)));
      return;
    }
    if ((event.key === "ArrowUp" || event.key === "ArrowDown") && perSeries) {
      event.preventDefault();
      const n = series.length;
      setKeyboard(true);
      setActive((a) => Math.min(a ?? last, last));
      setActiveSeries((k) => (event.key === "ArrowUp" ? (k + n - 1) % n : (k + 1) % n));
      return;
    }
    if (event.key === "Enter" || event.key === " ") {
      const href = hrefFor(active ?? last, perSeries ? seriesIdx : null);
      if (!href) return;
      setKeyboard(true);
      event.preventDefault();
      open(href);
    }
  };

  const keyHelp = `Use the left and right arrow keys to move between ${interval === "hour" ? "hours" : "days"}${perSeries ? ", up and down to choose a series" : ""}, and Enter to ${drillLabel.toLowerCase()}. With a screen reader, use the data table below ("Show data table"): it lists every value with its link.`;

  return (
    <figure
      className={`eap-viz rounded-lg border border-border bg-card text-card-foreground p-4 m-0 ${className}`}
      aria-labelledby={titleId}
      aria-describedby={summary ? summaryId : undefined}
      aria-busy={loading || undefined}
      data-testid={`chart-${title}`}
    >
      <div className="flex flex-wrap items-start justify-between gap-2 mb-2">
        <div>
          <figcaption id={titleId} className="text-sm font-semibold text-foreground">{title}</figcaption>
          {description && <p className="text-xs text-muted-foreground mt-0.5">{description}</p>}
        </div>
        {series.length >= 2 && (
          <ul className="flex flex-wrap gap-3 text-xs text-muted-foreground" aria-label={`${title} legend`}>
            {series.map((s) => (
              <li key={s.key} className="flex items-center gap-1.5">
                <span aria-hidden="true" className="inline-block w-2.5 h-2.5 rounded-sm" style={{ backgroundColor: s.color }} />
                {s.label}
              </li>
            ))}
          </ul>
        )}
      </div>

      {error ? (
        <div role="alert" className="flex items-center justify-center h-40 rounded-md border border-destructive bg-destructive/10 px-3 text-center text-sm text-foreground">
          {error}
        </div>
      ) : loading && buckets.length === 0 ? (
        <div className="h-40 rounded-md bg-muted animate-pulse" aria-label={`Loading ${title}`} />
      ) : empty ? (
        <div className="flex items-center justify-center h-40 text-sm text-muted-foreground">{emptyMessage}</div>
      ) : (
        <div
          className="relative rounded-md focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background"
          tabIndex={0}
          role="group"
          aria-label={`${title} chart. ${keyHelp}`}
          data-testid="chart-plot"
          onKeyDown={onKeyDown}
          onFocus={(event) => {
            // Keyboard focus selects the latest bucket; a mouse click (no :focus-visible) does not
            // enter keyboard mode, so its tooltip still follows the pointer and clears on leave.
            if (!isFocusVisible(event.currentTarget)) return;
            setKeyboard(true);
            setActive((a) => a ?? buckets.length - 1);
          }}
          onBlur={() => {
            setKeyboard(false);
            setActive(null);
          }}
          onMouseLeave={() => {
            if (!keyboard) setActive(null);
          }}
        >
          <svg
            viewBox={`0 0 ${W} ${H}`}
            width="100%"
            className={`block h-auto ${loading ? "opacity-60" : ""}`}
            aria-hidden="true"
            focusable="false"
          >
            {/* grid + y labels */}
            {ticks.map((t) => (
              <g key={t}>
                <line x1={M.left} x2={W - M.right} y1={geo.y(t)} y2={geo.y(t)} stroke="var(--viz-grid-color)" strokeWidth={1} />
                <text x={M.left - 6} y={geo.y(t)} dy="0.32em" textAnchor="end" fontSize={11} fill="var(--viz-axis-color)">
                  {formatValue(t)}
                </text>
              </g>
            ))}
            {/* x labels */}
            {xLabelIdx.map((i, n) => (
              <text
                key={i}
                x={geo.xCenter(i)}
                y={H - 8}
                fontSize={11}
                fill="var(--viz-axis-color)"
                textAnchor={xLabelIdx.length >= 3 ? (n === 0 ? "start" : n === xLabelIdx.length - 1 ? "end" : "middle") : "middle"}
              >
                {bucketLabel(i)}
              </text>
            ))}

            {/* active bucket highlight */}
            {active !== null && (
              <rect
                x={geo.xCenter(active) - geo.slot / 2}
                y={M.top}
                width={geo.slot}
                height={PLOT_H}
                fill="var(--viz-grid-color)"
                opacity={0.5}
                pointerEvents="none"
                data-testid="active-bucket"
                data-bucket={active}
              />
            )}

            {/* marks */}
            {type === "bar"
              ? series.map((s, k) =>
                  s.values.map((v, i) => {
                    if (!v) return null;
                    const x = geo.xCenter(i) - geo.group / 2 + k * (geo.bar + GAP);
                    const top = geo.y(v);
                    return (
                      <path
                        key={`${s.key}-${i}`}
                        d={barPath(x, top, geo.bar, M.top + PLOT_H - top)}
                        fill={s.color}
                        data-series={s.key}
                        pointerEvents="none"
                      />
                    );
                  }),
                )
              : series.map((s) => (
                  <g key={s.key} data-series={s.key} pointerEvents="none">
                    {linePaths(s.values, geo.xCenter, geo.y).map((d, j) => (
                      <path key={j} d={d} fill="none" stroke={s.color} strokeWidth={2} strokeLinejoin="round" strokeLinecap="round" />
                    ))}
                    {s.values.map((v, i) =>
                      v !== null && (buckets.length <= MARKER_LIMIT || i === active) ? (
                        <circle key={i} cx={geo.xCenter(i)} cy={geo.y(v)} r={4} fill={s.color} stroke="var(--viz-surface-color)" strokeWidth={2} />
                      ) : null,
                    )}
                  </g>
                ))}

            {/* keyboard: outline the selected series' bar slot */}
            {keyboard && perSeries && active !== null && (
              <rect
                x={geo.xCenter(active) - geo.group / 2 + seriesIdx * (geo.bar + GAP) - 1.5}
                y={M.top}
                width={geo.bar + 3}
                height={PLOT_H}
                fill="none"
                stroke="var(--viz-axis-color)"
                strokeWidth={1.5}
                rx={2}
                pointerEvents="none"
                data-testid="active-series"
              />
            )}

            {/* pointer hit targets (mouse only; keyboard uses the group above and the data table) */}
            {buckets.map((_, i) => {
              const targets = perSeries
                ? series.map((s, k) => ({
                    key: s.key,
                    k,
                    x: geo.xCenter(i) - geo.slot / 2 + (geo.slot / series.length) * k,
                    w: geo.slot / series.length,
                  }))
                : [{ key: "bucket", k: null as number | null, x: geo.xCenter(i) - geo.slot / 2, w: geo.slot }];
              return targets.map((t) => {
                const href = hrefFor(i, t.k);
                const hover = () => {
                  setKeyboard(false);
                  setActive(i);
                  if (t.k !== null) setActiveSeries(t.k);
                };
                const rect = <rect x={t.x} y={M.top} width={Math.max(1, t.w)} height={PLOT_H} fill="transparent" />;
                return href ? (
                  <a
                    key={`${t.key}-${i}`}
                    href={href}
                    tabIndex={-1}
                    onClick={navigateHandler(href, onNavigate)}
                    onMouseEnter={hover}
                    className="cursor-pointer"
                    data-bucket={i}
                    data-series={t.key}
                  >
                    {rect}
                  </a>
                ) : (
                  <g key={`${t.key}-${i}`} data-bucket={i} onMouseEnter={hover}>{rect}</g>
                );
              });
            })}
          </svg>

          <p aria-live="polite" aria-atomic="true" className="sr-only" data-testid="chart-live">
            {keyboard ? liveText : ""}
          </p>

          {active !== null && buckets[active] && (
            <div
              aria-hidden="true"
              data-testid="chart-tooltip"
              className="pointer-events-none absolute top-0 z-10 rounded-md border border-border bg-popover text-popover-foreground shadow px-2.5 py-1.5 text-xs whitespace-nowrap"
              style={{
                left: `${(geo.xCenter(active) / W) * 100}%`,
                transform: `translateX(${geo.xCenter(active) / W > 0.6 ? "-105%" : "5%"})`,
              }}
            >
              <div className="font-medium text-foreground mb-0.5">{bucketLabel(active)} UTC</div>
              {series.map((s, k) => (
                <div
                  key={s.key}
                  className={`flex items-center gap-1.5 ${perSeries && k === seriesIdx && keyboard ? "text-foreground font-medium" : "text-muted-foreground"}`}
                >
                  <span className="inline-block w-2 h-2 rounded-sm" style={{ backgroundColor: s.color }} />
                  <span>{s.label}</span>
                  <span className="ml-auto pl-3 tabular-nums text-foreground">{valueText(s.values[active], formatValue)}</span>
                </div>
              ))}
              {extraTooltip?.(active).map((line) => (
                <div key={line} className="text-muted-foreground">{line}</div>
              ))}
              {hrefFor(active, perSeries ? seriesIdx : null) && (
                <div className="mt-1 text-muted-foreground">Select to {drillLabel.toLowerCase()}</div>
              )}
            </div>
          )}
        </div>
      )}

      {summary && (
        <p id={summaryId} className="sr-only">{summary}</p>
      )}

      {!error && !empty && buckets.length > 0 && (
        <details className="mt-2 text-xs">
          <summary
            className="cursor-pointer rounded-sm text-muted-foreground hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            aria-label={`Show data table: ${title}`}
          >
            Show data table
          </summary>
          <div className="mt-2 max-h-64 overflow-auto">
            <table className="w-full text-xs tabular-nums">
              <caption className="sr-only">{title} by {interval} (UTC). Each {interval === "hour" ? "hour" : "day"} links to: {drillLabel.toLowerCase()}.</caption>
              <thead>
                <tr className="text-muted-foreground">
                  <th scope="col" className="text-left font-medium py-1 pr-3">{interval === "hour" ? "Hour" : "Day"} (UTC)</th>
                  {series.map((s) => (
                    <th key={s.key} scope="col" className="text-right font-medium py-1 pl-3">{s.label}</th>
                  ))}
                </tr>
              </thead>
              <tbody className="divide-y divide-border">
                {buckets.map((b, i) => {
                  const rowHref = bucketHref?.(i) ?? null;
                  return (
                    <tr key={b.start}>
                      <th scope="row" className="text-left font-normal py-1 pr-3">
                        {rowHref ? (
                          <a
                            href={rowHref}
                            onClick={navigateHandler(rowHref, onNavigate)}
                            className="underline-offset-2 hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring rounded-sm"
                            aria-label={`${bucketLabel(i)}: ${drillLabel}`}
                          >
                            {bucketLabel(i)}
                          </a>
                        ) : (
                          bucketLabel(i)
                        )}
                      </th>
                      {series.map((s) => {
                        const v = s.values[i];
                        const href = s.href && v ? s.href(i) : null;
                        return (
                          <td key={s.key} className="text-right py-1 pl-3">
                            {href ? (
                              <a
                                href={href}
                                onClick={navigateHandler(href, onNavigate)}
                                className="underline-offset-2 hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring rounded-sm"
                                aria-label={`${bucketLabel(i)}, ${s.label}: ${valueText(v, formatValue)}. ${drillLabel}`}
                              >
                                {valueText(v, formatValue)}
                              </a>
                            ) : v === null || v === undefined ? (
                              "—"
                            ) : (
                              formatValue(v)
                            )}
                          </td>
                        );
                      })}
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </details>
      )}
    </figure>
  );
}
