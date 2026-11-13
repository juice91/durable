"use client";

import Link from "next/link";
import React, { useCallback, useEffect, useRef, useState } from "react";
import { localInputToIso } from "@/lib/datetimeLocal";
import {
  formatMatchCounts,
  PROCESSOR_ACTIONS,
  PROCESSOR_PHASES,
  processors as processorsApi,
  type ProcessorAction,
  type ProcessorDeclaration,
  type ProcessorPhase,
  type ProcessorResultFilters,
  type ProcessorResultItem,
  type ProcessorType,
} from "@/lib/processors";

const inputClass = "w-full rounded-md border border-input bg-background px-2 py-1.5 text-sm text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring";
const secondaryButton = "rounded-md border border-border px-3 py-1.5 text-sm text-foreground hover:bg-accent disabled:cursor-not-allowed disabled:opacity-50";
const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
export const RESULTS_PAGE_SIZE = 50;

interface FilterForm {
  agent_id: string;
  type: ProcessorType | "";
  action: ProcessorAction | "";
  phase: ProcessorPhase | "";
  run_id: string;
  from: string;
  to: string;
}

const EMPTY_FILTERS: FilterForm = { agent_id: "", type: "", action: "", phase: "", run_id: "", from: "", to: "" };

function actionClass(action: string | null): string {
  if (action === "block") return "bg-destructive/10 text-destructive-text";
  if (action === "allow" || action === null) return "bg-muted text-muted-foreground";
  return "bg-primary/10 text-primary";
}

/** Form values as API filters; a message instead when they cannot be sent. */
function toFilters(form: FilterForm): ProcessorResultFilters | string {
  const runId = form.run_id.trim();
  if (runId && !UUID_PATTERN.test(runId)) return "Run ID must be a full run UUID.";
  const from = localInputToIso(form.from) ?? undefined;
  const to = localInputToIso(form.to) ?? undefined;
  if (from && to && from >= to) return "From must be before To.";
  return { agent_id: form.agent_id, type: form.type, action: form.action, phase: form.phase, run_id: runId, from, to };
}

/** Recorded processor outcomes, newest first, with filters, run links and paging. Counts only. */
export function ProcessorResultsTable({ agents, types }: {
  agents: { id: string; name: string }[];
  types: Pick<ProcessorDeclaration, "id" | "title">[];
}) {
  const [form, setForm] = useState<FilterForm>(EMPTY_FILTERS);
  const [applied, setApplied] = useState<ProcessorResultFilters>({});
  const [items, setItems] = useState<ProcessorResultItem[]>([]);
  const [nextBefore, setNextBefore] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadingMore, setLoadingMore] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [formError, setFormError] = useState<string | null>(null);
  // Ignores answers to superseded requests (filters changed while one was in flight).
  const generation = useRef(0);

  const agentName = (id: string) => agents.find((agent) => agent.id === id)?.name ?? id.slice(0, 8);
  const typeTitle = (id: ProcessorType | null) => (id ? types.find((t) => t.id === id)?.title ?? id : "unknown");

  const load = useCallback(async (filters: ProcessorResultFilters) => {
    const mine = ++generation.current;
    setLoading(true);
    setError(null);
    try {
      const page = await processorsApi.results({ ...filters, limit: RESULTS_PAGE_SIZE });
      if (mine !== generation.current) return;
      setItems(page.items);
      setNextBefore(page.next_before);
    } catch (reason: unknown) {
      if (mine !== generation.current) return;
      setItems([]);
      setNextBefore(null);
      setError(reason instanceof Error ? reason.message : "Failed to load processor outcomes");
    } finally {
      if (mine === generation.current) setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load(applied);
  }, [applied, load]);

  const loadMore = async () => {
    if (!nextBefore || loadingMore) return;
    const mine = generation.current;
    setLoadingMore(true);
    try {
      const page = await processorsApi.results({ ...applied, limit: RESULTS_PAGE_SIZE, before: nextBefore });
      if (mine !== generation.current) return;
      setItems((current) => [...current, ...page.items]);
      setNextBefore(page.next_before);
    } catch (reason: unknown) {
      if (mine === generation.current) setError(reason instanceof Error ? reason.message : "Failed to load more outcomes");
    } finally {
      setLoadingMore(false);
    }
  };

  const apply = (event: React.FormEvent) => {
    event.preventDefault();
    const filters = toFilters(form);
    if (typeof filters === "string") {
      setFormError(filters);
      return;
    }
    setFormError(null);
    setApplied(filters);
  };

  const reset = () => {
    setForm(EMPTY_FILTERS);
    setFormError(null);
    setApplied({});
  };

  const field = <K extends keyof FilterForm>(name: K) => ({
    value: form[name],
    onChange: (event: React.ChangeEvent<HTMLInputElement | HTMLSelectElement>) =>
      setForm((current) => ({ ...current, [name]: event.target.value })),
  });

  const filtered = Object.values(applied).some((value) => value !== undefined && value !== "");

  return (
    <div className="space-y-4">
      <form onSubmit={apply} aria-label="Filter outcomes" className="grid gap-3 rounded-md border border-border p-3 sm:grid-cols-2 lg:grid-cols-4">
        <div>
          <label htmlFor="filter-agent" className="block text-xs font-medium text-muted-foreground">Agent</label>
          <select id="filter-agent" className={inputClass} {...field("agent_id")}>
            <option value="">All agents</option>
            {agents.map((agent) => <option key={agent.id} value={agent.id}>{agent.name}</option>)}
          </select>
        </div>
        <div>
          <label htmlFor="filter-type" className="block text-xs font-medium text-muted-foreground">Processor</label>
          <select id="filter-type" className={inputClass} {...field("type")}>
            <option value="">All types</option>
            {types.map((type) => <option key={type.id} value={type.id}>{type.title}</option>)}
          </select>
        </div>
        <div>
          <label htmlFor="filter-action" className="block text-xs font-medium text-muted-foreground">Action</label>
          <select id="filter-action" className={inputClass} {...field("action")}>
            <option value="">All actions</option>
            {PROCESSOR_ACTIONS.map((action) => <option key={action} value={action}>{action}</option>)}
          </select>
        </div>
        <div>
          <label htmlFor="filter-phase" className="block text-xs font-medium text-muted-foreground">Phase</label>
          <select id="filter-phase" className={inputClass} {...field("phase")}>
            <option value="">Input and output</option>
            {PROCESSOR_PHASES.map((phase) => <option key={phase} value={phase}>{phase}</option>)}
          </select>
        </div>
        <div>
          <label htmlFor="filter-run" className="block text-xs font-medium text-muted-foreground">Run ID</label>
          <input id="filter-run" type="text" className={`${inputClass} font-mono`} placeholder="Run UUID" {...field("run_id")} />
        </div>
        <div>
          <label htmlFor="filter-from" className="block text-xs font-medium text-muted-foreground">From</label>
          <input id="filter-from" type="datetime-local" className={inputClass} {...field("from")} />
        </div>
        <div>
          <label htmlFor="filter-to" className="block text-xs font-medium text-muted-foreground">To</label>
          <input id="filter-to" type="datetime-local" className={inputClass} {...field("to")} />
        </div>
        <div className="flex items-end gap-2">
          <button type="submit" className="rounded-md bg-primary px-3 py-1.5 text-sm font-medium text-primary-foreground">Apply</button>
          <button type="button" className={secondaryButton} onClick={reset}>Reset</button>
        </div>
        {formError ? <p role="alert" data-testid="results-filter-error" className="text-sm text-destructive-text sm:col-span-2 lg:col-span-4">{formError}</p> : null}
      </form>

      <p className="text-xs text-muted-foreground">Outcomes record match counts only. Matched text is never stored, so it cannot be shown here.</p>

      {error ? <p role="alert" data-testid="results-error" className="rounded-md border border-destructive/30 bg-destructive/10 p-3 text-sm text-destructive-text">{error}</p> : null}
      {loading ? <p className="text-sm text-muted-foreground">Loading outcomes…</p> : null}

      {!loading && !error && items.length === 0 ? (
        <div data-testid="results-empty" className="rounded-lg border border-dashed border-border p-8 text-center">
          <p className="font-medium text-foreground">{filtered ? "No outcomes match these filters" : "No processor outcomes yet"}</p>
          <p className="mt-1 text-sm text-muted-foreground">
            {filtered ? "Change or reset the filters." : "Outcomes appear here once an agent with processors runs."}
          </p>
        </div>
      ) : null}

      {!loading && items.length > 0 ? (
        <div className="overflow-x-auto rounded-md border border-border">
          <table className="w-full text-left text-sm">
            <thead className="bg-muted/50 text-xs text-muted-foreground">
              <tr>
                <th scope="col" className="px-3 py-2 font-medium">Time</th>
                <th scope="col" className="px-3 py-2 font-medium">Agent</th>
                <th scope="col" className="px-3 py-2 font-medium">Run</th>
                <th scope="col" className="px-3 py-2 font-medium">Phase</th>
                <th scope="col" className="px-3 py-2 font-medium">Processor</th>
                <th scope="col" className="px-3 py-2 font-medium">Action</th>
                <th scope="col" className="px-3 py-2 font-medium">Matches</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-border">
              {items.map((item) => (
                <tr key={`${item.run_id}:${item.seq}`} data-testid="result-row" data-run-id={item.run_id}>
                  <td className="whitespace-nowrap px-3 py-2 text-muted-foreground">
                    <time dateTime={item.ts}>{new Date(item.ts).toLocaleString()}</time>
                  </td>
                  <td className="px-3 py-2">
                    <Link href={`/agents/${item.agent_id}`} className="text-primary hover:underline">{agentName(item.agent_id)}</Link>
                    {item.agent_version !== null ? <span className="ml-1 font-mono text-xs text-muted-foreground">v{item.agent_version}</span> : null}
                  </td>
                  <td className="px-3 py-2">
                    <Link href={`/traces/${item.run_id}`} className="font-mono text-xs text-primary hover:underline" aria-label={`Open trace of run ${item.run_id}`}>
                      {item.run_id.slice(0, 8)}
                    </Link>
                    {item.parent_run_id ? <span className="ml-1 text-xs text-muted-foreground">child run</span> : null}
                  </td>
                  <td className="px-3 py-2 text-foreground">{item.phase ?? "unknown"}</td>
                  <td className="px-3 py-2 text-foreground">
                    {typeTitle(item.processor_type)}
                    {item.index !== null ? <span className="ml-1 text-xs text-muted-foreground">#{item.index + 1}</span> : null}
                  </td>
                  <td className="px-3 py-2">
                    <span className={`rounded-full px-2 py-0.5 text-xs ${actionClass(item.action)}`}>{item.action ?? "unknown"}</span>
                  </td>
                  <td className="px-3 py-2 text-xs text-muted-foreground">
                    {item.processor_type === "moderation" && Object.keys(item.match_counts).length === 0
                      ? (item.categories && item.categories.length > 0 ? null : "No category flagged")
                      : formatMatchCounts(item.match_counts)}
                    {item.categories && item.categories.length > 0 ? <span className="block">Categories: {item.categories.join(", ")}</span> : null}
                    {item.reason ? <span className="block">Reason: {item.reason}</span> : null}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : null}

      {!loading && nextBefore ? (
        <button type="button" className={secondaryButton} disabled={loadingMore} onClick={() => void loadMore()}>
          {loadingMore ? "Loading…" : "Load older outcomes"}
        </button>
      ) : null}
    </div>
  );
}
