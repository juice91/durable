"use client";

import React, { useCallback, useEffect, useRef, useState } from "react";
import { ApiError } from "@/lib/api";
import { actorLabel, EMPTY_AUDIT_FILTERS, listAudit, type AuditFilters, type AuditOutcome, type AuditRow } from "@/lib/audit";
import { Badge, type BadgeProps } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";

const OUTCOME_VARIANT: Record<AuditOutcome, BadgeProps["variant"]> = { success: "success", denied: "warning", failed: "destructive" };
const inputClass =
  "h-8 min-w-0 rounded-md border border-border bg-surface px-2 text-label text-foreground placeholder:text-muted-foreground focus-visible:outline focus-visible:outline-2 focus-visible:outline-ring";

function when(iso: string): string {
  const t = Date.parse(iso);
  return Number.isNaN(t) ? iso : new Date(t).toLocaleString();
}

function Field({ id, label, children }: { id: string; label: string; children: React.ReactNode }) {
  return (
    <label htmlFor={id} className="flex min-w-0 flex-col gap-1 text-caption text-muted-foreground">
      {label}
      {children}
    </label>
  );
}

/** The Settings "Audit" tab (design 7.3): who changed what, filterable by actor, action, resource, outcome and time. */
export function AuditSection() {
  const [draft, setDraft] = useState<AuditFilters>(EMPTY_AUDIT_FILTERS);
  const [applied, setApplied] = useState<AuditFilters>(EMPTY_AUDIT_FILTERS);
  const [rows, setRows] = useState<AuditRow[]>([]);
  const [next, setNext] = useState<string | null>(null);
  const [retention, setRetention] = useState<number | null | undefined>(undefined);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const generation = useRef(0);

  const load = useCallback(async (filters: AuditFilters, cursor: string | null) => {
    const mine = ++generation.current;
    setLoading(true);
    setError(null);
    try {
      const page = await listAudit(filters, cursor);
      if (mine !== generation.current) return;
      setRows((current) => (cursor ? [...current, ...page.items] : page.items));
      setNext(page.next_cursor);
      setRetention(page.retention_days);
    } catch (reason) {
      if (mine !== generation.current) return;
      setError(reason instanceof ApiError && reason.status === 404 ? "The audit log is not available to you." : reason instanceof Error ? reason.message : "The audit log could not be loaded.");
    } finally {
      if (mine === generation.current) setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load(applied, null);
  }, [applied, load]);

  const set = (key: keyof AuditFilters) => (event: React.ChangeEvent<HTMLInputElement | HTMLSelectElement>) =>
    setDraft((current) => ({ ...current, [key]: event.target.value }));

  return (
    <section aria-labelledby="settings-audit" data-testid="settings-audit" className="flex min-w-0 flex-col gap-3 rounded-lg border border-border p-4">
      <div className="flex min-w-0 flex-col gap-1">
        <h2 id="settings-audit" className="text-title font-semibold text-foreground">Audit log</h2>
        <p className="text-label text-muted-foreground">
          Every change made through the API or the MCP endpoint: who did it, to what, and how it ended. Requests, secrets and keys are never recorded.
          {retention === undefined ? null : retention === null ? " Entries are kept without limit." : ` Entries are kept for ${retention} days.`}
        </p>
      </div>
      <form
        data-testid="audit-filters"
        className="grid min-w-0 grid-cols-1 gap-2 sm:grid-cols-2 lg:grid-cols-4"
        onSubmit={(event) => {
          event.preventDefault();
          setApplied(draft);
        }}
      >
        <Field id="audit-actor" label="Actor"><input id="audit-actor" className={inputClass} value={draft.actor} onChange={set("actor")} placeholder="user@example.com" /></Field>
        <Field id="audit-action" label="Action"><input id="audit-action" className={inputClass} value={draft.action} onChange={set("action")} placeholder="publish_version" /></Field>
        <Field id="audit-resource-kind" label="Resource"><input id="audit-resource-kind" className={inputClass} value={draft.resource_kind} onChange={set("resource_kind")} placeholder="agents" /></Field>
        <Field id="audit-resource-id" label="Resource id"><input id="audit-resource-id" className={inputClass} value={draft.resource_id} onChange={set("resource_id")} /></Field>
        <Field id="audit-outcome" label="Outcome">
          <select id="audit-outcome" className={inputClass} value={draft.outcome} onChange={set("outcome")}>
            <option value="">Any</option>
            <option value="success">Success</option>
            <option value="denied">Denied</option>
            <option value="failed">Failed</option>
          </select>
        </Field>
        <Field id="audit-since" label="From"><input id="audit-since" type="datetime-local" className={inputClass} value={draft.since} onChange={set("since")} /></Field>
        <Field id="audit-until" label="Until"><input id="audit-until" type="datetime-local" className={inputClass} value={draft.until} onChange={set("until")} /></Field>
        <div className="flex items-end gap-2">
          <Button type="submit" data-testid="audit-apply">Apply</Button>
          <Button type="button" variant="ghost" data-testid="audit-clear" onClick={() => { setDraft(EMPTY_AUDIT_FILTERS); setApplied(EMPTY_AUDIT_FILTERS); }}>Clear</Button>
        </div>
      </form>
      {error ? <p role="alert" data-testid="audit-error" className="text-label text-destructive-text">{error}</p> : null}
      <div className="min-w-0 overflow-x-auto">
        <table data-testid="audit-table" className="w-full md:min-w-[40rem] border-collapse text-left text-label">
          <thead>
            <tr className="border-b border-border text-caption text-muted-foreground">
              <th scope="col" className="py-1.5 pr-3 font-medium">When</th>
              <th scope="col" className="py-1.5 pr-3 font-medium">Actor</th>
              <th scope="col" className="py-1.5 pr-3 font-medium">Action</th>
              <th scope="col" className="py-1.5 pr-3 font-medium">Resource</th>
              <th scope="col" className="py-1.5 pr-3 font-medium">Outcome</th>
              <th scope="col" className="py-1.5 font-medium">Request</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <tr key={row.id} data-testid="audit-row" data-audit-id={row.id} className="border-b border-border align-top last:border-b-0">
                <td className="whitespace-nowrap py-1.5 pr-3 text-muted-foreground">{when(row.at)}</td>
                <td className="min-w-0 break-all py-1.5 pr-3 text-foreground">{actorLabel(row)}</td>
                <td className="py-1.5 pr-3 font-mono text-caption text-foreground">{row.action}</td>
                <td className="min-w-0 break-all py-1.5 pr-3 text-foreground">{row.resource_kind ?? "—"}{row.resource_id ? ` · ${row.resource_id}` : ""}</td>
                <td className="py-1.5 pr-3"><Badge variant={OUTCOME_VARIANT[row.outcome]}>{row.outcome}</Badge></td>
                <td className="min-w-0 break-all py-1.5 font-mono text-caption text-muted-foreground">{row.request_id ?? "—"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {!loading && !error && rows.length === 0 ? <p data-testid="audit-empty" className="text-label text-muted-foreground">No entries match.</p> : null}
      {loading ? <p data-testid="audit-loading" className="text-label text-muted-foreground">Loading…</p> : null}
      {next && !loading ? (
        <div><Button type="button" variant="secondary" data-testid="audit-more" onClick={() => void load(applied, next)}>Load more</Button></div>
      ) : null}
    </section>
  );
}
