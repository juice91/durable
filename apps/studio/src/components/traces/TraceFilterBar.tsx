"use client";

import React, { useEffect, useState } from "react";
import { isoToLocalInput, localInputToIso } from "@/lib/datetimeLocal";
import { RUN_KIND_LABELS, type RunKind } from "@/lib/runs";
import { hasActiveFilters, type TraceFilters } from "@/lib/traces";
import { Button } from "@/components/ui/Button";

const STATUS_OPTIONS: Array<{ value: string; label: string }> = [
  { value: "completed", label: "Completed" },
  { value: "failed", label: "Failed" },
  { value: "running", label: "Running" },
  { value: "queued", label: "Queued" },
  { value: "recovering", label: "Recovering" },
  { value: "needs_attention", label: "Needs Attention" },
];

const splitList = (value: string | null) => (value ? value.split(",").map((v) => v.trim()).filter(Boolean) : []);

export const filterInputClass =
  "h-8 rounded-md border border-input bg-surface px-2 text-label text-foreground placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring";
export const filterLabelClass = "text-caption font-medium text-muted-foreground";
const inputClass = filterInputClass;
const labelClass = filterLabelClass;

function TextFilter({ id, label, value, placeholder, onCommit }: {
  id: string; label: string; value: string | null; placeholder: string; onCommit: (value: string | null) => void;
}) {
  const [draft, setDraft] = useState(value ?? "");
  useEffect(() => setDraft(value ?? ""), [value]);
  const commit = () => {
    const next = draft.trim() || null;
    if (next !== value) onCommit(next);
  };
  return (
    <div className="flex min-w-0 flex-col gap-1">
      <label htmlFor={id} className={labelClass}>{label}</label>
      <input
        id={id}
        value={draft}
        placeholder={placeholder}
        onChange={(e) => setDraft(e.target.value)}
        onBlur={commit}
        onKeyDown={(e) => {
          if (e.key === "Enter") commit();
        }}
        className={`${inputClass} w-full sm:w-44`}
      />
    </div>
  );
}

export interface TraceFilterBarProps<F extends TraceFilters> {
  filters: F;
  setFilters: (patch: Partial<TraceFilters>) => void;
  /** Clear every filter the page has. */
  onClear: () => void;
  agentNames: Record<string, string>;
  /** The Search box's hint (what `q` matches on this page). */
  searchPlaceholder?: string;
  /** A filter the page has beyond TraceFilters is set (so Clear filters shows). */
  extraActive?: boolean;
  /** Extra controls after the Kind select (the Spans page's Span kind). */
  extra?: React.ReactNode;
  /** Right-aligned controls (the column chooser). */
  trailing?: React.ReactNode;
}

/** The Traces filter bar, shared by the Traces and Spans pages. */
export function TraceFilterBar<F extends TraceFilters>({ filters, setFilters, onClear, agentNames, searchPlaceholder = "input, output, error, model", extraActive = false, extra, trailing }: TraceFilterBarProps<F>) {
  const statusValue = filters.status.length === 0 ? "all" : filters.status.length === 1 ? filters.status[0] : filters.status.join(",");
  const customStatus = filters.status.length > 1 || (filters.status.length === 1 && !STATUS_OPTIONS.some((o) => o.value === filters.status[0]));
  const agentOptions = Object.entries(agentNames).sort((a, b) => a[1].localeCompare(b[1]));
  return (
    <div className="flex min-w-0 flex-wrap items-end gap-3 rounded-lg border border-border bg-surface p-3" role="search" aria-label="Trace filters" data-testid="traces-filter-bar">
      <div className="flex min-w-0 flex-col gap-1">
        <label htmlFor="status-filter" className={labelClass}>
          Filter by status:
        </label>
        <select
          id="status-filter"
          value={statusValue}
          onChange={(e) => setFilters({ status: e.target.value === "all" ? [] : e.target.value.split(",") })}
          className={inputClass}
        >
          <option value="all">All</option>
          {STATUS_OPTIONS.map((o) => (
            <option key={o.value} value={o.value}>{o.label}</option>
          ))}
          {customStatus ? <option value={statusValue}>{filters.status.join(", ").replace(/_/g, " ")}</option> : null}
        </select>
      </div>
      <div className="flex min-w-0 flex-col gap-1">
        <label htmlFor="kind-filter" className={labelClass}>Kind</label>
        <select
          id="kind-filter"
          value={filters.kind ?? "all"}
          onChange={(e) => setFilters({ kind: e.target.value === "all" ? null : (e.target.value as TraceFilters["kind"]) })}
          className={inputClass}
        >
          <option value="all">All</option>
          {(Object.keys(RUN_KIND_LABELS) as RunKind[]).map((kind) => (
            <option key={kind} value={kind}>{RUN_KIND_LABELS[kind]}</option>
          ))}
        </select>
      </div>
      {extra}
      <div className="flex min-w-0 flex-col gap-1">
        <label htmlFor="agent-filter" className={labelClass}>Agent</label>
        <select
          id="agent-filter"
          value={filters.agent_id ?? ""}
          onChange={(e) => setFilters({ agent_id: e.target.value || null })}
          className={`${inputClass} max-w-48`}
        >
          <option value="">All agents</option>
          {agentOptions.map(([id, name]) => (
            <option key={id} value={id}>{name}</option>
          ))}
          {filters.agent_id && !agentNames[filters.agent_id] ? (
            <option value={filters.agent_id}>{filters.agent_id.slice(0, 8)}...</option>
          ) : null}
        </select>
      </div>
      <TextFilter id="model-filter" label="Model" value={filters.model} placeholder="provider:name" onCommit={(model) => setFilters({ model })} />
      <TextFilter id="provider-filter" label="Provider" value={filters.provider} placeholder="e.g. openai" onCommit={(provider) => setFilters({ provider })} />
      <div className="flex min-w-0 flex-col gap-1">
        <label htmlFor="from-filter" className={labelClass}>From</label>
        <input
          id="from-filter"
          type="datetime-local"
          value={isoToLocalInput(filters.from)}
          onChange={(e) => setFilters({ from: localInputToIso(e.target.value) })}
          className={inputClass}
        />
      </div>
      <div className="flex min-w-0 flex-col gap-1">
        <label htmlFor="to-filter" className={labelClass}>To</label>
        <input
          id="to-filter"
          type="datetime-local"
          value={isoToLocalInput(filters.to)}
          onChange={(e) => setFilters({ to: localInputToIso(e.target.value) })}
          className={inputClass}
        />
      </div>
      <TextFilter id="q-filter" label="Search" value={filters.q} placeholder={searchPlaceholder} onCommit={(q) => setFilters({ q })} />
      <TextFilter id="tags-filter" label="Tags" value={filters.tags.length ? filters.tags.join(", ") : null} placeholder="kind:agent, draft" onCommit={(value) => setFilters({ tags: splitList(value) })} />
      <TextFilter id="metadata-filter" label="Metadata" value={filters.metadata.length ? filters.metadata.join(", ") : null} placeholder="key=value, key=value" onCommit={(value) => setFilters({ metadata: splitList(value).filter((pair) => pair.includes("=")) })} />
      {hasActiveFilters(filters) || extraActive ? (
        <Button variant="ghost" size="sm" onClick={onClear}>
          Clear filters
        </Button>
      ) : null}
      {trailing ? <div className="ml-auto">{trailing}</div> : null}
    </div>
  );
}
