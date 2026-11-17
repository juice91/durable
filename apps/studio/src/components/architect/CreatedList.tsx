"use client";

import React from "react";
import { Plus } from "lucide-react";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { cn } from "@/lib/cn";
import { ICON_STROKE_WIDTH } from "@/lib/icons";
import type { CreatedItem } from "@/lib/architect";

export interface CreatedListProps {
  agents: readonly CreatedItem[];
  workflows: readonly CreatedItem[];
  /** The agent slug open in the form, if any. */
  selectedAgentId: string | null;
  onNewAgent: () => void;
  onSelectAgent: (item: CreatedItem) => void;
}

function Row({ item, selected, onSelect }: { item: CreatedItem; selected: boolean; onSelect?: () => void }) {
  const content = (
    <>
      <span className="min-w-0 flex-1 truncate font-mono text-label">{item.id} · v{item.versions}</span>
      {item.draft ? <Badge variant="warning" data-testid="architect-draft-marker">Draft</Badge> : null}
      {item.archived ? <Badge variant="neutral">Archived</Badge> : null}
    </>
  );
  const rowClass = cn("flex w-full min-w-0 items-center gap-2 rounded-md px-2 py-1.5 text-start", selected ? "bg-muted text-foreground" : "text-foreground");
  return (
    <li title={item.name}>
      {onSelect ? (
        <button type="button" onClick={onSelect} aria-current={selected ? "true" : undefined} className={cn(rowClass, "hover:bg-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring")}>
          {content}
        </button>
      ) : (
        <div className={rowClass}>{content}</div>
      )}
    </li>
  );
}

/** The left column: "+ New agent", then what the Architect page created, each as `id · vN`. */
export function CreatedList({ agents, workflows, selectedAgentId, onNewAgent, onSelectAgent }: CreatedListProps) {
  return (
    <div data-testid="architect-created" className="flex min-w-0 flex-col gap-4">
      <Button variant="primary" onClick={onNewAgent} data-testid="architect-new-agent">
        <Plus strokeWidth={ICON_STROKE_WIDTH} aria-hidden="true" />
        New agent
      </Button>
      <section aria-labelledby="architect-created-agents" className="min-w-0">
        <h2 id="architect-created-agents" className="mb-1 px-2 text-caption font-medium uppercase tracking-wide text-muted-foreground">Agents</h2>
        {agents.length === 0 ? (
          <p className="px-2 text-label text-muted-foreground">Nothing created yet.</p>
        ) : (
          <ul className="space-y-0.5">
            {agents.map((item) => (
              <Row key={item.id} item={item} selected={item.id === selectedAgentId} onSelect={() => onSelectAgent(item)} />
            ))}
          </ul>
        )}
      </section>
      <section aria-labelledby="architect-created-workflows" className="min-w-0">
        <h2 id="architect-created-workflows" className="mb-1 px-2 text-caption font-medium uppercase tracking-wide text-muted-foreground">Workflows</h2>
        {workflows.length === 0 ? (
          <p className="px-2 text-label text-muted-foreground">Nothing created yet.</p>
        ) : (
          <ul className="space-y-0.5">
            {workflows.map((item) => (
              <Row key={item.id} item={item} selected={false} />
            ))}
          </ul>
        )}
      </section>
    </div>
  );
}
