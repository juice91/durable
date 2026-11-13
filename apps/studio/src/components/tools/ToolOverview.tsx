"use client";

import React from "react";
import Link from "next/link";
import { Button } from "@/components/ui/Button";
import { describeUsage, type AgentSpecsStatus, type AgentUsage } from "@/lib/agentUsage";
import { routes } from "@/lib/routes";

export interface ToolPolicy {
  sideEffect: boolean;
  allowed: boolean;
}

export interface ToolOverviewProps {
  description: string | null;
  /** The input schema; null when it is not known here (an MCP tool listed from agent specs). */
  inputSchema: Record<string, unknown> | null;
  /** Shown instead of the input schema when it is null. */
  inputSchemaNote?: React.ReactNode;
  /** The output schema, when the source declares one. */
  outputSchema?: Record<string, unknown> | null;
  /** Built-in tools: the catalog's side-effect and allowlist flags. */
  policy?: ToolPolicy | null;
  usedBy: AgentUsage[];
  usageStatus: AgentSpecsStatus;
  /** Some published versions could not be read. */
  usagePartial?: boolean;
  onRetryUsage?: () => void;
  /** Where it is shown: the MCP server page's tool Sheet keeps its own schema test id. */
  context?: "tools" | "mcp";
}

const sectionTitle = "text-caption font-medium text-muted-foreground";
const preClass = "max-h-[60vh] overflow-auto rounded-md bg-muted p-3 font-mono text-caption text-foreground";

function Section({ title, testId, children }: { title: string; testId?: string; children: React.ReactNode }) {
  return (
    <section className="min-w-0 space-y-1" data-testid={testId}>
      <h3 className={sectionTitle}>{title}</h3>
      {children}
    </section>
  );
}

/**
 * A tool, read-only: description, policy, input and output schema, and the agents that use it.
 * The Tools page's Overview tab, and the MCP server page's tool Sheet.
 */
export function ToolOverview({
  description,
  inputSchema,
  inputSchemaNote,
  outputSchema = null,
  policy = null,
  usedBy,
  usageStatus,
  usagePartial = false,
  onRetryUsage,
  context = "tools",
}: ToolOverviewProps) {
  return (
    <div className="min-w-0 space-y-4" data-testid="tool-overview">
      <Section title="Description">
        <p className="text-body text-foreground">{description || "No description."}</p>
      </Section>

      {policy ? (
        <Section title="Policy" testId="tool-policy">
          <dl className="grid grid-cols-[auto_minmax(0,1fr)] gap-x-4 gap-y-1 text-body">
            <dt className="text-muted-foreground">Effects</dt>
            <dd className="text-foreground">{policy.sideEffect ? "Changes data outside Studio" : "Reads only"}</dd>
            <dt className="text-muted-foreground">Allowlist</dt>
            <dd className={policy.allowed ? "text-foreground" : "text-destructive-text"}>{policy.allowed ? "Allowed on this platform" : "Not allowed: the API refuses runs"}</dd>
          </dl>
        </Section>
      ) : null}

      <Section title="Input schema">
        {inputSchema ? (
          <pre data-testid={context === "mcp" ? "mcp-tool-schema" : "tool-input-schema"} className={preClass}>{JSON.stringify(inputSchema, null, 2)}</pre>
        ) : (
          <p className="text-body text-muted-foreground">{inputSchemaNote ?? "Not known."}</p>
        )}
      </Section>

      <Section title="Output schema">
        {outputSchema ? (
          <pre data-testid="tool-output-schema" className={preClass}>{JSON.stringify(outputSchema, null, 2)}</pre>
        ) : (
          <p className="text-body text-muted-foreground">Not declared. Run the tool to see its output.</p>
        )}
      </Section>

      <Section title="Used by agents" testId="tool-used-by">
        <div className="rounded-md border border-border" data-status={usageStatus}>
          {usageStatus === "loading" ? (
            <p className="p-3 text-body text-muted-foreground">Checking which agents use it…</p>
          ) : usageStatus === "error" ? (
            <div className="flex flex-wrap items-center justify-between gap-3 p-3 text-body text-muted-foreground">
              <span>Could not load agents.</span>
              {onRetryUsage ? <Button size="sm" onClick={onRetryUsage}>Retry</Button> : null}
            </div>
          ) : usedBy.length === 0 ? (
            <p className="p-3 text-body text-muted-foreground">No agent uses this tool.</p>
          ) : (
            <ul className="divide-y divide-border">
              {usedBy.map((item) => (
                <li key={item.agentId} data-testid="tool-used-by-agent" className="flex min-w-0 flex-wrap items-center justify-between gap-2 px-3 py-2">
                  <Link href={routes.agentEditor(item.agentId)} className="min-w-0 truncate font-medium text-foreground underline-offset-4 hover:underline">
                    {item.agentName}
                  </Link>
                  <span className="text-caption text-muted-foreground">{describeUsage(item)}</span>
                </li>
              ))}
            </ul>
          )}
          {usageStatus === "ready" && usagePartial ? (
            <p className="border-t border-border px-3 py-2 text-caption text-muted-foreground">Some published versions could not be read; only their drafts were checked.</p>
          ) : null}
        </div>
      </Section>
    </div>
  );
}
