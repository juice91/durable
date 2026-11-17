"use client";

import Link from "next/link";
import React from "react";
import { Badge } from "@/components/ui/Badge";
import {
  describeSchemaType,
  PROCESSOR_PHASE_LABELS,
  PROCESSOR_PHASES,
  type ProcessorCatalog,
  type ProcessorDeclaration,
  type ProcessorUsage,
} from "@/lib/processors";
import { routes } from "@/lib/routes";

/** The scope note every processor page carries (P0.21): what processors do not see. */
export const PROCESSOR_SCOPE_NOTE =
  "Input and output processors check the user's input and the model's output; step processors check tool calls and their results; stream processors filter the answer as it streams. Request context values are not processed; moderation checks only the final answer.";

/** Agents that use one processor type: latest published version (or draft only), and phases. */
export function ProcessorUsageList({ usage, loading, error }: { usage: ProcessorUsage[] | undefined; loading: boolean; error: string | null }) {
  if (loading) return <p className="text-caption text-muted-foreground">Loading agents…</p>;
  if (error) return <p role="alert" className="text-caption text-destructive-text">{error}</p>;
  if (!usage || usage.length === 0) return <p data-testid="processor-usage-empty" className="text-caption text-muted-foreground">No agent uses this processor yet.</p>;
  return (
    <ul data-testid="processor-usage" className="divide-y divide-border">
      {usage.map((item) => (
        <li key={item.agent_id} className="flex min-w-0 flex-wrap items-center gap-2 py-2 text-label">
          <Link href={routes.agentEditor(item.agent_id)} className="min-w-0 truncate font-medium text-foreground hover:underline">{item.agent_name}</Link>
          {item.unknown ? (
            <span data-testid="processor-usage-unknown" className="text-caption text-muted-foreground">
              unknown: v{item.version} could not be read
            </span>
          ) : (
            <>
              <Badge variant="outline" className="font-mono">{item.version === null ? "draft only" : `v${item.version}`}</Badge>
              <span className="text-caption text-muted-foreground">{item.phases.join(" and ")}</span>
            </>
          )}
        </li>
      ))}
    </ul>
  );
}

/** One mark per phase (Input, Step, Stream, Output): a badge per supported phase, a muted struck-through one for the others. */
export function PhaseMarks({ phases }: { phases: ProcessorDeclaration["phases"] }) {
  return (
    <span className="inline-flex gap-1">
      <span className="sr-only">{phases.length > 0 ? `Phases: ${phases.join(", ")}` : "No phases"}</span>
      {PROCESSOR_PHASES.map((phase) =>
        phases.includes(phase) ? (
          <Badge key={phase} variant="outline" aria-hidden="true">{PROCESSOR_PHASE_LABELS[phase]}</Badge>
        ) : (
          <Badge key={phase} variant="neutral" aria-hidden="true" className="opacity-50 line-through">{PROCESSOR_PHASE_LABELS[phase]}</Badge>
        ),
      )}
    </span>
  );
}

/** The limits that apply to one type, from the catalog (RE2 patterns for the blocklist). */
function typeLimits(item: ProcessorDeclaration, limits: ProcessorCatalog["limits"]): string[] {
  const lines = [`At most ${limits.max_processors_per_phase} processors per phase.`];
  if (item.id === "regex_blocklist") {
    lines.push(
      `RE2 syntax (linear time; no backreferences or lookaround), at most ${limits.max_regex_patterns} patterns of ${limits.max_regex_pattern_length} characters.`,
    );
  }
  return lines;
}

/**
 * One processor type: what it does, phases, actions, limits, its config fields and JSON schema.
 * Keeps the `processor-card` test id (one card, for the page's type).
 */
export function ProcessorOverview({ item, limits }: { item: ProcessorDeclaration; limits: ProcessorCatalog["limits"] }) {
  const properties = Object.entries(item.config_schema.properties || {});
  const required = item.config_schema.required || [];
  return (
    <section
      aria-labelledby="processor-overview"
      data-testid="processor-card"
      data-processor-type={item.id}
      className="flex min-w-0 flex-col gap-3 rounded-lg border border-border bg-surface p-4"
    >
      <div className="flex min-w-0 flex-wrap items-start justify-between gap-2">
        <div className="min-w-0">
          <h2 id="processor-overview" className="text-title font-semibold text-foreground">Overview</h2>
          <p className="font-mono text-caption text-muted-foreground">{item.id}</p>
        </div>
        {item.deterministic ? null : <Badge variant="neutral">Calls a model · test in the chat</Badge>}
      </div>
      <p className="text-body text-foreground">{item.description}</p>
      <dl className="grid grid-cols-[auto_minmax(0,1fr)] gap-x-3 gap-y-1 text-label">
        <dt className="text-muted-foreground">Phases</dt>
        <dd className="text-foreground">{item.phases.join(", ")}</dd>
        <dt className="text-muted-foreground">Actions</dt>
        <dd className="text-foreground">{item.actions.join(", ")}</dd>
        <dt className="text-muted-foreground">Limits</dt>
        <dd data-testid="processor-limits" className="text-foreground">
          {typeLimits(item, limits).map((line) => <span key={line} className="block">{line}</span>)}
        </dd>
      </dl>
      <p data-testid="processor-scope-note" className="text-caption text-muted-foreground">{PROCESSOR_SCOPE_NOTE}</p>
      <div className="min-w-0">
        <h3 className="mb-1 text-caption font-medium uppercase tracking-wide text-muted-foreground">Configuration</h3>
        {properties.length === 0 ? <p className="text-caption text-muted-foreground">None</p> : (
          <ul data-testid="processor-config-fields" className="space-y-1 text-label">
            {properties.map(([name, schema]) => (
              <li key={name} className="min-w-0 break-words">
                <span className="font-mono text-foreground">{name}</span>
                {required.includes(name) ? <span className="text-destructive-text"> *</span> : null}
                <span className="text-muted-foreground"> · {describeSchemaType(schema)}</span>
                {schema.default !== undefined ? <span className="text-muted-foreground"> · default {JSON.stringify(schema.default)}</span> : null}
                {schema.description ? <span className="block text-caption text-muted-foreground">{schema.description}</span> : null}
              </li>
            ))}
          </ul>
        )}
        <details className="mt-2">
          <summary className="cursor-pointer text-caption text-muted-foreground">Config JSON schema</summary>
          <pre className="mt-1 max-h-64 overflow-auto rounded-md bg-muted p-2 font-mono text-caption text-foreground">{JSON.stringify(item.config_schema, null, 2)}</pre>
        </details>
      </div>
    </section>
  );
}
