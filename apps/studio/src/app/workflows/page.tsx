"use client";

import React, { useEffect, useState } from "react";
import Link from "next/link";
import { EmptyState } from "@/components/shared/EmptyState";
import { formatDateTime } from "@/components/workflows/ApprovalCard";
import { getWorkflowVersion, listWorkflows, type Workflow } from "@/lib/workflows";
import { routes } from "@/lib/routes";
import { PageHeader } from "@/components/frame/PageHeader";
import { navCrumb } from "@/lib/nav";

interface Row extends Workflow {
  /** When the latest version was published (the API has no updated_at for a workflow). */
  updatedAt: string | null;
}

export default function WorkflowsPage() {
  const [rows, setRows] = useState<Row[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    listWorkflows()
      .then(async (workflows) => {
        if (cancelled) return;
        setRows(workflows.map((w) => ({ ...w, updatedAt: null })));
        const published = await Promise.all(workflows.map(async (w) => {
          if (w.latest_version == null) return null;
          try {
            return (await getWorkflowVersion(w.id, w.latest_version)).created_at;
          } catch {
            return null;
          }
        }));
        if (!cancelled) setRows(workflows.map((w, i) => ({ ...w, updatedAt: published[i] })));
      })
      .catch((reason: unknown) => {
        if (!cancelled) setError(reason instanceof Error ? reason.message : "Failed to load workflows");
      });
    return () => {
      cancelled = true;
    };
  }, []);

  return (
    <>
      <PageHeader crumbs={[navCrumb("workflows", { current: true })]} />
    <div className="mx-auto max-w-7xl px-4 py-8">
      <div className="mb-6">
        <p className="mt-1 text-muted-foreground">Published workflow definitions: open one to see its execution graph, run it and follow its runs.</p>
      </div>
      {error ? <p role="alert" className="rounded-md border border-destructive/30 bg-destructive/10 p-3 text-sm text-destructive-text">{error}</p> : null}
      {!rows && !error ? <p className="text-muted-foreground">Loading workflows…</p> : null}
      {rows && rows.length === 0 ? (
        <EmptyState title="No workflows yet" description="Create a workflow definition through the API (POST /api/v1/workflows) and publish a version to run it here." />
      ) : null}
      {rows && rows.length > 0 ? (
        <div className="overflow-x-auto rounded-lg border border-border bg-card">
          <table className="w-full text-left text-sm" aria-label="Workflows">
            <thead className="border-b border-border text-xs uppercase text-muted-foreground">
              <tr>
                <th scope="col" className="px-4 py-3">Name</th>
                <th scope="col" className="px-4 py-3">Latest version</th>
                <th scope="col" className="px-4 py-3">Updated</th>
                <th scope="col" className="px-4 py-3"><span className="sr-only">Open</span></th>
              </tr>
            </thead>
            <tbody>
              {rows.map((w) => (
                <tr key={w.id} data-testid={`workflow-row-${w.id}`} className="border-t border-border hover:bg-accent/40">
                  <td className="px-4 py-3">
                    <Link href={routes.workflow(w.id)} className="font-medium text-foreground underline-offset-4 hover:underline">{w.name}</Link>
                    {w.draft_spec?.description ? <p className="line-clamp-1 text-xs text-muted-foreground">{w.draft_spec.description}</p> : null}
                  </td>
                  <td className="px-4 py-3">{w.latest_version != null ? `v${w.latest_version}` : <span className="text-muted-foreground">Draft only</span>}</td>
                  <td className="px-4 py-3 text-muted-foreground" title="When the latest version was published">{w.updatedAt ? formatDateTime(w.updatedAt) : "—"}</td>
                  <td className="px-4 py-3 text-right">
                    <Link href={routes.workflow(w.id)} aria-label={`Open ${w.name}`} className="text-primary underline-offset-4 hover:underline">Open</Link>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : null}
    </div>
    </>
  );
}
