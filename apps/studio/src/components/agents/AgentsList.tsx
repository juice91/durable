"use client";

import React, { useId, useState } from "react";
import { EmptyState } from "@/components/shared/EmptyState";
import { Card } from "@/components/agents/Card";

interface Agent {
  id: string;
  name: string;
  latest_version: number | null;
}

interface AgentsListUIProps {
  agents: Agent[];
  onSelect: (id: string) => void;
}

/** Case-insensitive name match; an empty query matches every agent. */
export function filterAgents<T extends { name: string }>(agents: T[], query: string): T[] {
  const needle = query.trim().toLocaleLowerCase();
  if (!needle) return agents;
  return agents.filter((agent) => agent.name.toLocaleLowerCase().includes(needle));
}

export function AgentsListUI({ agents, onSelect }: AgentsListUIProps) {
  const [query, setQuery] = useState("");
  const searchId = useId();

  if (agents.length === 0) {
    return (
      <EmptyState
        title="No agents yet"
        description="Create your first agent to get started."
      />
    );
  }

  const visible = filterAgents(agents, query);

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-3">
        <label htmlFor={searchId} className="sr-only">Search agents</label>
        <input
          id={searchId}
          type="search"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="Search agents by name"
          className="w-full max-w-sm rounded-md border border-input bg-background px-3 py-2 text-sm text-foreground placeholder:text-muted-foreground focus:outline-none focus:ring-2 focus:ring-ring"
        />
        <p role="status" aria-live="polite" className="text-sm text-muted-foreground">
          {query.trim() ? `${visible.length} of ${agents.length} agents` : `${agents.length} agent${agents.length !== 1 ? "s" : ""}`}
        </p>
      </div>
      {visible.length === 0 ? (
        <EmptyState
          title="No matching agents"
          description={`No agent name contains "${query.trim()}".`}
        />
      ) : (
        <div className="grid gap-4">
          {visible.map((agent) => (
            <Card
              key={agent.id}
              className="p-4 hover:bg-accent cursor-pointer focus:outline-none focus:ring-2 focus:ring-accent-foreground rounded-md"
              onClick={() => onSelect(agent.id)}
              tabIndex={0}
              onKeyDown={(e) => {
                if (e.key === "Enter" || e.key === " ") {
                  e.preventDefault();
                  onSelect(agent.id);
                }
              }}
            >
              <div className="flex items-center justify-between">
                <div>
                  <h3 className="font-medium text-card-foreground">{agent.name}</h3>
                  <p className="text-sm text-muted-foreground">
                    {agent.latest_version !== null ? `Version ${agent.latest_version}` : "Draft"}
                  </p>
                </div>
                <span className="text-muted-foreground">→</span>
              </div>
            </Card>
          ))}
        </div>
      )}
    </div>
  );
}
