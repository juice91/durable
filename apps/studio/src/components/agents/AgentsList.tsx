"use client";

import React, { useEffect, useId, useState } from "react";
import Link from "next/link";
import { LayoutGrid, List } from "lucide-react";
import type { AgentSpec } from "@/lib/api";
import { cn } from "@/lib/cn";
import { ICON_STROKE_WIDTH } from "@/lib/icons";
import { routes } from "@/lib/routes";
import { DataList, type DataListColumn } from "@/components/shared/DataList";
import { EmptyState } from "@/components/shared/EmptyState";
import { focusRing } from "@/components/ui/Button";
import { SegmentedControl } from "@/components/ui/SegmentedControl";
import { Tooltip, TooltipProvider } from "@/components/ui/Tooltip";
import { agentPurpose, readAgentsView, writeAgentsView, type AgentsView } from "@/components/agentPage/agentPageModel";

/** What the list needs of `GET /agents` (the spec is optional so a bare row still renders). */
export interface AgentListItem {
  id: string;
  name: string;
  latest_version: number | null;
  draft_spec?: AgentSpec;
}

export interface AgentsListUIProps {
  agents: AgentListItem[];
  loading?: boolean;
  error?: string | null;
  onRetry?: () => void;
}

export const AGENTS_SEARCH_PLACEHOLDER = "Search by name or instructions";
const EMPTY = { title: "No agents yet", description: "Create your first agent to get started." };

/** Case-insensitive match on the name or the instructions; an empty query matches every agent. */
export function filterAgents<T extends { name: string; draft_spec?: AgentSpec }>(agents: T[], query: string): T[] {
  const needle = query.trim().toLocaleLowerCase();
  if (!needle) return agents;
  return agents.filter(
    (agent) =>
      agent.name.toLocaleLowerCase().includes(needle) || (agent.draft_spec?.instructions ?? "").toLocaleLowerCase().includes(needle),
  );
}

function modelOf(agent: AgentListItem): string | null {
  const model = agent.draft_spec?.model;
  return model ? `${model.provider}:${model.name}` : null;
}

function toolNames(agent: AgentListItem): string[] {
  const spec = agent.draft_spec;
  if (!spec) return [];
  return [...(spec.tools ?? []), ...(spec.mcp_tools ?? []).map((ref) => `${ref.tool} (MCP ${ref.server})`)];
}

function versionLabel(agent: AgentListItem): string {
  return agent.latest_version !== null ? `v${agent.latest_version}` : "Draft only";
}

/** The tool count, with the names in a tooltip (a focusable trigger, so keyboard users get it too). */
function ToolCount({ agent }: { agent: AgentListItem }) {
  const names = toolNames(agent);
  if (names.length === 0) return <span className="tabular-nums text-muted-foreground">0</span>;
  return (
    <Tooltip content={names.join(", ")}>
      <button type="button" aria-label={`${names.length} tools: ${names.join(", ")}`} className={cn("rounded-sm tabular-nums underline decoration-dotted underline-offset-2", focusRing)}>
        {names.length}
      </button>
    </Tooltip>
  );
}

const columns: DataListColumn<AgentListItem>[] = [
  { id: "name", header: "Name", cell: (agent) => agent.name, sortable: true },
  {
    id: "purpose",
    header: "Purpose",
    cell: (agent) => <span className="line-clamp-1 text-muted-foreground">{agentPurpose(agent.draft_spec)}</span>,
    sortValue: (agent) => agentPurpose(agent.draft_spec),
    sortable: true,
    className: "hidden max-w-md md:table-cell",
    headerClassName: "hidden md:table-cell",
  },
  {
    id: "model",
    header: "Model",
    cell: (agent) => <span className="font-mono text-caption">{modelOf(agent) ?? "-"}</span>,
    sortValue: (agent) => modelOf(agent),
    sortable: true,
    className: "hidden lg:table-cell",
    headerClassName: "hidden lg:table-cell",
  },
  { id: "tools", header: "Tools", cell: (agent) => <ToolCount agent={agent} />, sortValue: (agent) => toolNames(agent).length, sortable: true, align: "end" },
  {
    id: "version",
    header: "Version",
    cell: (agent) => versionLabel(agent),
    sortValue: (agent) => agent.latest_version ?? -1,
    sortable: true,
  },
];

/**
 * The agents list (decision "Agents list"): a DataList with search and sortable columns, or a compact
 * card grid; the choice persists in `localStorage["studio.agents.view"]`. A row opens the agent's chat.
 */
export function AgentsListUI({ agents, loading = false, error = null, onRetry }: AgentsListUIProps) {
  // Read after mount: the server render cannot know the stored choice.
  const [view, setView] = useState<AgentsView>("list");
  const [query, setQuery] = useState("");
  const searchId = useId();
  useEffect(() => setView(readAgentsView()), []);
  const changeView = (next: AgentsView) => {
    setView(next);
    writeAgentsView(next);
  };

  const matching = filterAgents(agents, query).length;
  const count =
    agents.length > 0 ? (
      <span data-testid="agents-count" className="text-caption tabular-nums text-muted-foreground">
        {query.trim() ? `${matching} of ${agents.length} agents` : `${agents.length} agent${agents.length === 1 ? "" : "s"}`}
      </span>
    ) : null;

  const viewToggle = (
    <SegmentedControl<AgentsView>
      label="Agents view"
      size="sm"
      value={view}
      onValueChange={changeView}
      options={[
        { value: "list", label: "List", icon: <List strokeWidth={ICON_STROKE_WIDTH} aria-hidden="true" /> },
        { value: "compact", label: "Compact", icon: <LayoutGrid strokeWidth={ICON_STROKE_WIDTH} aria-hidden="true" /> },
      ]}
    />
  );

  if (view === "list") {
    return (
      <TooltipProvider>
        <DataList<AgentListItem>
          label="Agents"
          rows={agents}
          columns={columns}
          getRowId={(agent) => agent.id}
          getRowHref={(agent) => routes.agentChat(agent.id)}
          getRowLabel={(agent) => agent.name}
          getRowTestId={() => "agent-row"}
          searchPlaceholder={AGENTS_SEARCH_PLACEHOLDER}
          searchLabel="Search agents"
          initialQuery={query}
          onSearchChange={setQuery}
          filterRow={(agent, q) => filterAgents([agent], q).length > 0}
          toolbar={
            <>
              {count}
              {viewToggle}
            </>
          }
          defaultSort={{ columnId: "name", direction: "asc" }}
          loading={loading}
          error={error}
          onRetry={onRetry}
          empty={EMPTY}
          noMatchText="No agent name or instructions match this search."
          testId="agents-list"
        />
      </TooltipProvider>
    );
  }

  const visible = filterAgents(agents, query);
  return (
    <div className="flex min-w-0 flex-col gap-3" data-testid="agents-grid">
      <div className="flex min-w-0 flex-wrap items-center gap-2">
        <label htmlFor={searchId} className="sr-only">
          Search agents
        </label>
        <input
          id={searchId}
          type="search"
          value={query}
          onChange={(event) => setQuery(event.target.value)}
          placeholder={AGENTS_SEARCH_PLACEHOLDER}
          className={cn("h-8 min-w-0 flex-1 basis-56 rounded-md border border-input bg-background px-3 text-label text-foreground placeholder:text-muted-foreground", focusRing)}
        />
        {count}
        {viewToggle}
      </div>
      {loading && agents.length === 0 ? (
        <p role="status" className="text-label text-muted-foreground">
          Loading agents…
        </p>
      ) : error ? (
        <p role="alert" className="text-label text-destructive-text">
          {error}
        </p>
      ) : agents.length === 0 ? (
        <EmptyState title={EMPTY.title} description={EMPTY.description} />
      ) : visible.length === 0 ? (
        <EmptyState title="No matching agents" description="No agent name or instructions match this search." />
      ) : (
        <ul className="grid gap-3" style={{ gridTemplateColumns: "repeat(auto-fit, minmax(min(100%, 16rem), 1fr))" }}>
          {visible.map((agent) => (
            <li key={agent.id} className="min-w-0">
              <Link
                href={routes.agentChat(agent.id)}
                data-testid="agent-card"
                className={cn("flex h-full min-w-0 flex-col gap-1 rounded-lg border border-border bg-surface p-3 hover:bg-fill-hover", focusRing)}
              >
                <span className="truncate font-medium text-foreground">{agent.name}</span>
                <span className="line-clamp-2 text-label text-muted-foreground">{agentPurpose(agent.draft_spec)}</span>
                <span className="mt-auto flex flex-wrap gap-x-3 text-caption text-muted-foreground">
                  <span className="font-mono">{modelOf(agent) ?? "-"}</span>
                  <span>{toolNames(agent).length} tools</span>
                  <span>{versionLabel(agent)}</span>
                </span>
              </Link>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
