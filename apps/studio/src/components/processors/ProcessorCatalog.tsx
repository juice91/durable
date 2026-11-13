"use client";

import Link from "next/link";
import React from "react";
import { describeSchemaType, type ProcessorDeclaration, type ProcessorType, type ProcessorUsage } from "@/lib/processors";
import { routes } from "@/lib/routes";

const secondaryButton = "rounded-md border border-border px-3 py-1.5 text-sm text-foreground hover:bg-accent disabled:cursor-not-allowed disabled:opacity-50";

function UsageList({ usage, loading, error }: { usage: ProcessorUsage[] | undefined; loading: boolean; error: string | null }) {
  if (loading) return <p className="text-xs text-muted-foreground">Loading agents…</p>;
  if (error) return <p className="text-xs text-destructive-text">{error}</p>;
  if (!usage || usage.length === 0) return <p data-testid="processor-usage-empty" className="text-xs text-muted-foreground">No agent uses this processor yet.</p>;
  return (
    <ul data-testid="processor-usage" className="space-y-1">
      {usage.map((item) => (
        <li key={item.agent_id} className="flex flex-wrap items-center gap-2 text-sm">
          <Link href={routes.agentEditor(item.agent_id)} className="font-medium text-primary hover:underline">{item.agent_name}</Link>
          {item.unknown ? (
            <span data-testid="processor-usage-unknown" className="text-xs text-muted-foreground">
              unknown: v{item.version} could not be read
            </span>
          ) : (
            <>
              <span className="rounded bg-muted px-1.5 py-0.5 font-mono text-xs text-muted-foreground">
                {item.version === null ? "draft only" : `v${item.version}`}
              </span>
              <span className="text-xs text-muted-foreground">{item.phases.join(" and ")}</span>
            </>
          )}
        </li>
      ))}
    </ul>
  );
}

/** One card per built-in processor type: what it does, its config schema and who uses it. */
export function ProcessorCatalog({ items, usage, usageLoading, usageError, onTry }: {
  items: ProcessorDeclaration[];
  usage: Record<ProcessorType, ProcessorUsage[]> | null;
  usageLoading: boolean;
  usageError: string | null;
  onTry: (type: ProcessorType) => void;
}) {
  return (
    <ul aria-label="Processor types" className="grid gap-4 md:grid-cols-2">
      {items.map((item) => {
        const properties = Object.entries(item.config_schema.properties || {});
        const required = item.config_schema.required || [];
        return (
          <li key={item.id} data-testid="processor-card" data-processor-type={item.id} className="flex flex-col gap-3 rounded-lg border border-border bg-card p-4">
            <div className="flex flex-wrap items-start justify-between gap-2">
              <div className="min-w-0">
                <h3 className="font-semibold text-card-foreground">{item.title}</h3>
                <p className="font-mono text-xs text-muted-foreground">{item.id}</p>
              </div>
              {item.deterministic ? (
                <button type="button" className={secondaryButton} onClick={() => onTry(item.id)}>Try in test console</button>
              ) : (
                <span className="rounded-full bg-muted px-2 py-0.5 text-xs text-muted-foreground">Calls a model · test in the playground</span>
              )}
            </div>
            <p className="text-sm text-foreground">{item.description}</p>
            <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 text-xs">
              <dt className="text-muted-foreground">Phases</dt>
              <dd className="text-foreground">{item.phases.join(", ")}</dd>
              <dt className="text-muted-foreground">Actions</dt>
              <dd className="text-foreground">{item.actions.join(", ")}</dd>
            </dl>
            <div>
              <h4 className="mb-1 text-xs font-medium uppercase tracking-wide text-muted-foreground">Configuration</h4>
              {properties.length === 0 ? <p className="text-xs text-muted-foreground">None</p> : (
                <ul data-testid="processor-config-fields" className="space-y-1 text-xs">
                  {properties.map(([name, schema]) => (
                    <li key={name}>
                      <span className="font-mono text-foreground">{name}</span>
                      {required.includes(name) ? <span className="text-destructive-text"> *</span> : null}
                      <span className="text-muted-foreground"> · {describeSchemaType(schema)}</span>
                      {schema.default !== undefined ? <span className="text-muted-foreground"> · default {JSON.stringify(schema.default)}</span> : null}
                      {schema.description ? <span className="block text-muted-foreground">{schema.description}</span> : null}
                    </li>
                  ))}
                </ul>
              )}
              <details className="mt-2">
                <summary className="cursor-pointer text-xs text-muted-foreground">Config JSON schema</summary>
                <pre className="mt-1 max-h-64 overflow-auto rounded-md bg-muted p-2 text-xs text-foreground">{JSON.stringify(item.config_schema, null, 2)}</pre>
              </details>
            </div>
            <div className="border-t border-border pt-3">
              <h4 className="mb-1 text-xs font-medium uppercase tracking-wide text-muted-foreground">Used by</h4>
              <UsageList usage={usage?.[item.id]} loading={usageLoading} error={usageError} />
            </div>
          </li>
        );
      })}
    </ul>
  );
}
