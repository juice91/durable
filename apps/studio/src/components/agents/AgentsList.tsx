import React from "react";
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

export function AgentsListUI({ agents, onSelect }: AgentsListUIProps) {
  if (agents.length === 0) {
    return (
      <EmptyState
        title="No agents yet"
        description="Create your first agent to get started."
      />
    );
  }

  return (
    <div className="grid gap-4">
      {agents.map((agent) => (
        <Card
          key={agent.id}
          className="p-4 hover:bg-gray-50 cursor-pointer focus:outline-none focus:ring-2 focus:ring-blue-500 rounded-md"
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
              <h3 className="font-medium text-gray-900">{agent.name}</h3>
              <p className="text-sm text-gray-500">
                {agent.latest_version !== null ? `Version ${agent.latest_version}` : "Draft"}
              </p>
            </div>
            <span className="text-gray-400">→</span>
          </div>
        </Card>
      ))}
    </div>
  );
}
