import React from "react";
import { formatCost, formatDuration, formatTokens, type TraceTotals as Totals } from "@/lib/traces";

function Stat({ label, value, hint, testId }: { label: string; value: string; hint?: string; testId: string }) {
  return (
    <div className="rounded-md border border-border bg-card px-3 py-2" data-testid={testId}>
      <p className="text-xs text-muted-foreground">{label}</p>
      <p className="font-mono text-base font-semibold text-card-foreground">{value}</p>
      {hint ? <p className="text-xs text-muted-foreground">{hint}</p> : null}
    </div>
  );
}

/** Trace totals: duration, counts, tokens and cost (with "no price" / "not recorded" spelled out). */
export function TraceTotals({ totals }: { totals: Totals }) {
  const cost = formatCost(totals.cost_usd);
  let costValue = cost ?? "-";
  let costHint: string | undefined;
  if (cost === null && totals.model_requests > 0) {
    if (totals.unrecorded_requests === totals.model_requests) {
      costValue = "not recorded";
    } else if (totals.unpriced_requests > 0 || totals.unrecorded_requests > 0) {
      costValue = "no price";
      const parts = [];
      if (totals.unpriced_requests) parts.push(`${totals.unpriced_requests} unpriced`);
      if (totals.unrecorded_requests) parts.push(`${totals.unrecorded_requests} not recorded`);
      costHint = `${parts.join(", ")} of ${totals.model_requests} requests`;
    }
  }
  const tokens = (v: number | null) => (v === null ? (totals.model_requests > 0 ? "not recorded" : "-") : formatTokens(v));
  const cache = (totals.cache_read_tokens ?? 0) + (totals.cache_write_tokens ?? 0);
  return (
    <div className="grid grid-cols-2 gap-2 sm:grid-cols-3 lg:grid-cols-6" aria-label="Trace totals" role="group">
      <Stat testId="totals-duration" label="Duration" value={formatDuration(totals.duration_ms)} />
      <Stat
        testId="totals-requests"
        label="Model requests"
        value={formatTokens(totals.model_requests)}
        hint={`${totals.tool_calls} tool call${totals.tool_calls === 1 ? "" : "s"}, ${totals.runs} run${totals.runs === 1 ? "" : "s"}`}
      />
      <Stat testId="totals-input-tokens" label="Input tokens" value={tokens(totals.input_tokens)} />
      <Stat testId="totals-output-tokens" label="Output tokens" value={tokens(totals.output_tokens)} />
      <Stat
        testId="totals-cache-tokens"
        label="Cache tokens"
        value={totals.cache_read_tokens === null && totals.cache_write_tokens === null ? "-" : formatTokens(cache)}
        hint={totals.cache_read_tokens !== null ? `${formatTokens(totals.cache_read_tokens)} read, ${formatTokens(totals.cache_write_tokens)} write` : undefined}
      />
      <Stat testId="totals-cost" label="Cost" value={costValue} hint={costHint} />
    </div>
  );
}
