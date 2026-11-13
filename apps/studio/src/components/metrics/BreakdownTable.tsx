"use client";

import React from "react";
import Link from "next/link";
import {
  FAILED_STATUSES,
  formatCost,
  formatCount,
  formatLatency,
  formatPercent,
  tracesHref,
  type BreakdownBy,
  type BreakdownItem,
  type BreakdownSort,
  type MetricsFilters,
} from "@/lib/metrics";

export interface BreakdownTableProps {
  title: string;
  by: BreakdownBy;
  items: readonly BreakdownItem[] | null;
  filters: MetricsFilters;
  sort: BreakdownSort;
  onSortChange?: (sort: BreakdownSort) => void;
  /** Client-side navigation when a row (outside its links) is clicked. */
  onNavigate?: (href: string) => void;
  loading?: boolean;
  error?: string | null;
  emptyMessage?: string;
  note?: string;
}

export const SORT_OPTIONS: { value: BreakdownSort; label: string }[] = [
  { value: "runs", label: "Runs" },
  { value: "failed", label: "Failed" },
  { value: "requests", label: "Requests" },
  { value: "tokens", label: "Tokens" },
  { value: "cost", label: "Cost" },
];

/** The display name of a breakdown row: the agent's name, its id if deleted, or the model id. */
export function breakdownLabel(item: BreakdownItem, by: BreakdownBy): string {
  if (by === "model") return item.key;
  return item.label ?? `Deleted agent (${item.key.slice(0, 8)})`;
}

/** The drill-down for a row: the Traces list filtered to this agent or model. */
export function breakdownHref(item: BreakdownItem, by: BreakdownBy, filters: MetricsFilters, failedOnly = false): string {
  const narrowed = by === "agent" ? { agent_id: item.key } : { model: item.key };
  return tracesHref(filters, failedOnly ? { ...narrowed, status: FAILED_STATUSES } : narrowed);
}

function CostCell({ item }: { item: BreakdownItem }) {
  if (item.requests > 0 && item.unpriced_requests >= item.requests) {
    return <span className="text-muted-foreground">no price</span>;
  }
  return (
    <>
      {formatCost(item.cost_usd)}
      {item.unpriced_requests > 0 && (
        <span className="block text-[11px] text-muted-foreground">+{formatCount(item.unpriced_requests)} unpriced</span>
      )}
    </>
  );
}

export function BreakdownTable({
  title,
  by,
  items,
  filters,
  sort,
  onSortChange,
  onNavigate,
  loading = false,
  error = null,
  emptyMessage = "No data in this period",
  note,
}: BreakdownTableProps) {
  const sortId = `breakdown-sort-${by}`;
  const th = "px-3 py-2 font-medium text-muted-foreground whitespace-nowrap";
  return (
    <section className="rounded-lg border border-border bg-card text-card-foreground" aria-labelledby={`breakdown-title-${by}`} aria-busy={loading || undefined}>
      <div className="flex flex-wrap items-center justify-between gap-2 px-4 pt-4 pb-2">
        <h2 id={`breakdown-title-${by}`} className="text-sm font-semibold text-foreground">{title}</h2>
        {onSortChange && (
          <label htmlFor={sortId} className="flex items-center gap-2 text-xs text-muted-foreground">
            Top by
            <select
              id={sortId}
              value={sort}
              onChange={(e) => onSortChange(e.target.value as BreakdownSort)}
              className="rounded-md border border-input bg-background px-2 py-1 text-xs text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            >
              {SORT_OPTIONS.map((o) => (
                <option key={o.value} value={o.value}>{o.label}</option>
              ))}
            </select>
          </label>
        )}
      </div>
      {note && <p className="px-4 pb-2 text-xs text-muted-foreground">{note}</p>}
      {error ? (
        <p role="alert" className="mx-4 mb-4 rounded-md border border-destructive bg-destructive/10 p-3 text-sm text-foreground">{error}</p>
      ) : loading && !items ? (
        <div className="mx-4 mb-4 h-32 rounded-md bg-muted animate-pulse" aria-label={`Loading ${title}`} />
      ) : !items || items.length === 0 ? (
        <p className="px-4 pb-6 pt-2 text-sm text-muted-foreground">{emptyMessage}</p>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full text-sm tabular-nums">
            <thead className="bg-muted/50">
              <tr>
                <th scope="col" className={`${th} text-left`}>{by === "agent" ? "Agent" : "Model"}</th>
                <th scope="col" className={`${th} text-right`}>Runs</th>
                <th scope="col" className={`${th} text-right`}>Failed</th>
                <th scope="col" className={`${th} text-right`}>Error rate</th>
                <th scope="col" className={`${th} text-right`}>p95</th>
                <th scope="col" className={`${th} text-right`}>Tokens</th>
                <th scope="col" className={`${th} text-right`}>Cost</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-border">
              {items.map((item) => {
                const href = breakdownHref(item, by, filters);
                const name = breakdownLabel(item, by);
                return (
                  <tr
                    key={item.key}
                    className="hover:bg-muted/50 transition-colors cursor-pointer"
                    data-testid={`breakdown-row-${by}-${item.key}`}
                    onClick={(e) => {
                      if (!onNavigate || (e.target as HTMLElement).closest("a")) return;
                      onNavigate(href);
                    }}
                  >
                    <th scope="row" className="px-3 py-2 text-left font-medium max-w-[16rem]">
                      <Link href={href} className="block truncate text-foreground hover:underline" title={by === "model" ? item.key : `${name} (${item.key})`} aria-label={`${name}: open matching runs`}>
                        {name}
                      </Link>
                    </th>
                    <td className="px-3 py-2 text-right whitespace-nowrap">{formatCount(item.runs)}</td>
                    <td className="px-3 py-2 text-right whitespace-nowrap">
                      {item.failed > 0 ? (
                        <Link href={breakdownHref(item, by, filters, true)} className="hover:underline" aria-label={`${name}: open ${item.failed} failed runs`}>
                          {formatCount(item.failed)}
                        </Link>
                      ) : (
                        "0"
                      )}
                    </td>
                    <td className="px-3 py-2 text-right whitespace-nowrap">{formatPercent(item.error_rate)}</td>
                    <td className="px-3 py-2 text-right whitespace-nowrap">{formatLatency(item.latency_p95_ms)}</td>
                    <td className="px-3 py-2 text-right whitespace-nowrap">{formatCount(item.input_tokens + item.output_tokens)}</td>
                    <td className="px-3 py-2 text-right whitespace-nowrap"><CostCell item={item} /></td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}
