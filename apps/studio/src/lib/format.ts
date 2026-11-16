// The one cost and token formatting rule used by the playground footer, traces, metrics and the
// dashboard, so the same run reads the same everywhere.

/** Parse the API's decimal cost string (or a number); null for missing or non-numeric values. */
export function parseCost(value: string | number | null | undefined): number | null {
  if (value === null || value === undefined || value === "") return null;
  const n = typeof value === "number" ? value : Number(value);
  return Number.isFinite(n) ? n : null;
}

/** The DB scale of costs (NUMERIC(.., 10)): never show more decimals than that. */
const MAX_COST_DECIMALS = 10;

/**
 * USD amount: "$0" for zero; below $1 two significant digits ("$0.015", "$0.0015", "$0.50");
 * at $1 and above two decimals with grouping ("$1.50", "$1,234.50").
 */
export function formatUsd(value: number): string {
  if (!Number.isFinite(value)) return "-";
  if (value === 0) return "$0";
  const sign = value < 0 ? "-" : "";
  const abs = Math.abs(value);
  if (abs < 1) {
    const decimals = Math.min(MAX_COST_DECIMALS, 1 - Math.floor(Math.log10(abs)));
    const text = abs.toFixed(decimals);
    // 0.996 rounds up to "1.0": fall through to the dollars-and-cents form.
    if (Number(text) < 1) return `${sign}$${text}`;
  }
  return `${sign}$${abs.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
}

/** A cost from the API (decimal string or number) via `formatUsd`; null when missing or unparsable,
 * so each caller can say why ("no price", "not recorded", "—"). */
export function formatCost(value: string | number | null | undefined): string | null {
  const n = parseCost(value);
  return n === null ? null : formatUsd(n);
}

/** A score (evaluator verdicts 0..1, retrieval hits) with `digits` decimals: "0.75", "0.500".
 * The one placeholder for a missing score is "-", as for tokens and durations. */
export function formatScore(value: unknown, digits = 2): string {
  return typeof value === "number" && Number.isFinite(value) ? value.toFixed(digits) : "-";
}

/** Token count: exact with grouping below a million ("999,999"), compact above ("1.2M", "3.4B");
 * "-" when missing. */
export function formatTokens(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return "-";
  const abs = Math.abs(value);
  const compact = (n: number) => n.toFixed(1).replace(/\.0$/, "");
  if (abs >= 1e9) return `${compact(value / 1e9)}B`;
  if (abs >= 1e6) return `${compact(value / 1e6)}M`;
  return Math.round(value).toLocaleString("en-US");
}

/**
 * A duration as Studio shows it everywhere (traces, spans, tool calls, tool runs, workflow runs):
 * "120 ms", "1.25 s" (two decimals under 10 s), "12.5 s", "2 min 5 s", "1 h 5 min"; "-" when there
 * is none.
 */
export function formatDuration(ms: number | null | undefined): string {
  if (ms === null || ms === undefined || !Number.isFinite(ms) || ms < 0) return "-";
  if (ms < 1000) return `${Math.round(ms)} ms`;
  if (ms < 60_000) return `${(ms / 1000).toFixed(ms < 10_000 ? 2 : 1)} s`;
  const totalSeconds = Math.round(ms / 1000);
  const minutes = Math.floor(totalSeconds / 60);
  if (minutes < 60) {
    const seconds = totalSeconds % 60;
    return seconds ? `${minutes} min ${seconds} s` : `${minutes} min`;
  }
  const hours = Math.floor(minutes / 60);
  const rest = minutes % 60;
  return rest ? `${hours} h ${rest} min` : `${hours} h`;
}

/** Milliseconds from `startIso` to `endIso`, or null when either is missing or the span is negative. */
export function durationBetween(startIso: string | null | undefined, endIso: string | null | undefined): number | null {
  if (!startIso || !endIso) return null;
  const ms = new Date(endIso).getTime() - new Date(startIso).getTime();
  return Number.isFinite(ms) && ms >= 0 ? ms : null;
}
