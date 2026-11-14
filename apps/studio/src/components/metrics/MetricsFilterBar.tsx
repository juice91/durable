"use client";

import React, { useEffect, useState } from "react";
import { isoToLocalInput, localInputToIso } from "@/lib/datetimeLocal";
import {
  DEFAULT_PAGE_STATE,
  RANGE_PRESETS,
  bucketCount,
  presetRange,
  validateRange,
  MAX_BUCKETS,
  OPERATION_LABELS,
  type MetricsKind,
  type MetricsOperation,
  type MetricsPageState,
  type RangePreset,
} from "@/lib/metrics";

export interface MetricsFilterBarProps {
  state: MetricsPageState;
  onChange: (next: MetricsPageState) => void;
  /** The resolved range (for the custom inputs' initial values and the hourly limit). */
  range: { from: string; to: string };
  agents?: readonly { id: string; name: string }[];
  models?: readonly string[];
  providers?: readonly string[];
  /** Hide the dimension filters (date range only). */
  dimensions?: boolean;
  /** Show the interval selector. */
  showInterval?: boolean;
  onRefresh?: () => void;
}

const control =
  "rounded-md border border-input bg-background px-2.5 py-1.5 text-sm text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:opacity-50";
const labelCls = "flex flex-col gap-1 text-xs font-medium text-muted-foreground";

export function MetricsFilterBar({
  state,
  onChange,
  range,
  agents = [],
  models = [],
  providers = [],
  dimensions = true,
  showInterval = true,
  onRefresh,
}: MetricsFilterBarProps) {
  const [customFrom, setCustomFrom] = useState(() => isoToLocalInput(state.from ?? range.from));
  const [customTo, setCustomTo] = useState(() => isoToLocalInput(state.to ?? range.to));
  const [customError, setCustomError] = useState<string | null>(null);

  useEffect(() => {
    if (state.range === "custom") {
      setCustomFrom(isoToLocalInput(state.from));
      setCustomTo(isoToLocalInput(state.to));
    }
  }, [state.range, state.from, state.to]);

  const hourlyTooMany = bucketCount(range.from, range.to, "hour") > MAX_BUCKETS;
  const set = (patch: Partial<MetricsPageState>) => onChange({ ...state, ...patch });

  const onRange = (value: RangePreset) => {
    setCustomError(null);
    if (value === "custom") {
      // Start the custom range from what is shown now.
      const base = state.range === "custom" ? range : presetRange(state.range);
      set({ range: "custom", from: base.from, to: base.to, interval: fixInterval(state.interval, base) });
      return;
    }
    set({ range: value, from: null, to: null, interval: fixInterval(state.interval, presetRange(value)) });
  };

  const applyCustom = (e: React.FormEvent) => {
    e.preventDefault();
    const from = localInputToIso(customFrom);
    const to = localInputToIso(customTo);
    const problem = !from || !to ? "Enter a valid start and end time" : validateRange(from, to);
    if (problem) {
      setCustomError(problem);
      return;
    }
    setCustomError(null);
    set({ range: "custom", from, to, interval: fixInterval(state.interval, { from: from!, to: to! }) });
  };

  const modelOptions = unique([...(state.model ? [state.model] : []), ...models]);
  const providerOptions = unique([...(state.provider ? [state.provider] : []), ...providers]);
  const agentKnown = !state.agent_id || agents.some((a) => a.id === state.agent_id);
  const active = dimensions && (state.agent_id || state.model || state.provider || state.kind || state.operation);

  return (
    <div className="rounded-lg border border-border bg-card p-3" role="group" aria-label="Metrics filters">
      <div className="flex flex-wrap items-end gap-3">
        <label className={labelCls}>
          Date range
          <select aria-label="Date range" className={control} value={state.range} onChange={(e) => onRange(e.target.value as RangePreset)}>
            {RANGE_PRESETS.map((p) => (
              <option key={p.value} value={p.value}>{p.label}</option>
            ))}
            <option value="custom">Custom range</option>
          </select>
        </label>

        {state.range === "custom" && (
          <form className="flex flex-wrap items-end gap-2" onSubmit={applyCustom} aria-label="Custom range">
            <label className={labelCls}>
              From (local time)
              <input type="datetime-local" aria-label="From" className={control} value={customFrom} onChange={(e) => setCustomFrom(e.target.value)} />
            </label>
            <label className={labelCls}>
              To (local time)
              <input type="datetime-local" aria-label="To" className={control} value={customTo} onChange={(e) => setCustomTo(e.target.value)} />
            </label>
            <button type="submit" className="rounded-md border border-input bg-secondary px-3 py-1.5 text-sm text-secondary-foreground hover:bg-accent">
              Apply
            </button>
          </form>
        )}

        {showInterval && (
          <label className={labelCls}>
            Interval
            <select
              aria-label="Interval"
              className={control}
              value={state.interval ?? "auto"}
              onChange={(e) => set({ interval: e.target.value === "auto" ? null : (e.target.value as "hour" | "day") })}
            >
              <option value="auto">Auto</option>
              <option value="hour" disabled={hourlyTooMany}>Hourly{hourlyTooMany ? " (range too long)" : ""}</option>
              <option value="day">Daily</option>
            </select>
          </label>
        )}

        {dimensions && (
          <>
            <label className={labelCls}>
              Agent
              <select aria-label="Agent" className={control} value={state.agent_id ?? ""} onChange={(e) => set({ agent_id: e.target.value || null })}>
                <option value="">All agents</option>
                {!agentKnown && <option value={state.agent_id!}>{state.agent_id!.slice(0, 8)}…</option>}
                {agents.map((a) => (
                  <option key={a.id} value={a.id}>{a.name}</option>
                ))}
              </select>
            </label>
            <label className={labelCls}>
              Model
              <select aria-label="Model" className={control} value={state.model ?? ""} onChange={(e) => set({ model: e.target.value || null })}>
                <option value="">All models</option>
                {modelOptions.map((m) => (
                  <option key={m} value={m}>{m}</option>
                ))}
              </select>
            </label>
            <label className={labelCls}>
              Provider
              <select aria-label="Provider" className={control} value={state.provider ?? ""} onChange={(e) => set({ provider: e.target.value || null })}>
                <option value="">All providers</option>
                {providerOptions.map((p) => (
                  <option key={p} value={p}>{p}</option>
                ))}
              </select>
            </label>
            <label className={labelCls}>
              Kind
              <select aria-label="Kind" className={control} value={state.kind ?? ""} onChange={(e) => set({ kind: (e.target.value || null) as MetricsKind | null })}>
                <option value="">All kinds</option>
                <option value="agent">Agent</option>
                <option value="workflow">Workflow</option>
                <option value="tool">Tool</option>
                <option value="kb_ingest">Knowledge ingest</option>
              </select>
            </label>
            <label className={labelCls}>
              Operation
              <select
                aria-label="Operation"
                className={control}
                value={state.operation ?? ""}
                onChange={(e) => set({ operation: (e.target.value || null) as MetricsOperation | null })}
              >
                <option value="">All operations</option>
                {(Object.keys(OPERATION_LABELS) as MetricsOperation[]).map((op) => (
                  <option key={op} value={op}>{OPERATION_LABELS[op]}</option>
                ))}
              </select>
            </label>
          </>
        )}

        <div className="ml-auto flex items-end gap-2">
          {active && (
            <button
              type="button"
              className="rounded-md px-3 py-1.5 text-sm text-muted-foreground hover:text-foreground hover:bg-accent"
              onClick={() => onChange({ ...DEFAULT_PAGE_STATE, range: state.range, from: state.from, to: state.to, interval: state.interval })}
            >
              Clear filters
            </button>
          )}
          {onRefresh && (
            <button type="button" className="rounded-md border border-input bg-background px-3 py-1.5 text-sm text-foreground hover:bg-accent" onClick={onRefresh}>
              Refresh
            </button>
          )}
        </div>
      </div>
      {customError && (
        <p role="alert" className="mt-2 text-xs font-medium text-foreground">{customError}</p>
      )}
    </div>
  );
}

/** Drop an hourly interval the new range cannot hold (the API would 422). */
function fixInterval(interval: MetricsPageState["interval"], range: { from: string; to: string }): MetricsPageState["interval"] {
  return interval === "hour" && bucketCount(range.from, range.to, "hour") > MAX_BUCKETS ? null : interval;
}

function unique(values: string[]): string[] {
  return Array.from(new Set(values.filter(Boolean)));
}
