"use client";

import React, { useEffect, useState } from "react";
import { Plus } from "lucide-react";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuTrigger } from "@/components/ui/DropdownMenu";
import { cn } from "@/lib/cn";
import { ICON_STROKE_WIDTH } from "@/lib/icons";
import type { CreatedItem } from "@/lib/architect";
import { ProvenanceFooter } from "./ProvenanceFooter";

export const ARCHITECT_SHOW_ARCHIVED_KEY = "studio.architect.show-archived";

function readShowArchived(): boolean {
  try {
    return window.localStorage.getItem(ARCHITECT_SHOW_ARCHIVED_KEY) === "true";
  } catch {
    return false;
  }
}

export interface CreatedListProps {
  agents: readonly CreatedItem[];
  workflows: readonly CreatedItem[];
  /** The agent slug open in the form, if any. */
  selectedAgentId: string | null;
  /** "New" was chosen: start a fresh agent form or a fresh workflow form. */
  onNew: (kind: "agent" | "workflow") => void;
  onSelectAgent: (item: CreatedItem) => void;
}

function Row({ item, kind, selected, onSelect }: { item: CreatedItem; kind: "agent" | "workflow"; selected: boolean; onSelect?: () => void }) {
  const content = (
    <>
      <span className="min-w-0 flex-1">
        <span className="block truncate text-label">{item.name}</span>
        <span className="block truncate font-mono text-caption text-muted-foreground">{item.id} · v{item.versions}</span>
      </span>
      {item.draft ? <Badge variant="warning" data-testid="architect-draft-marker">Draft</Badge> : null}
      {item.archived ? <Badge variant="neutral">Archived</Badge> : null}
    </>
  );
  const rowClass = cn("flex w-full min-w-0 items-center gap-2 rounded-md px-2 py-1.5 text-start", selected ? "bg-muted text-foreground" : "text-foreground");
  return (
    <li title={`${item.name} (${item.id})`}>
      {onSelect ? (
        <button type="button" onClick={onSelect} aria-current={selected ? "true" : undefined} className={cn(rowClass, "hover:bg-muted focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring")}>
          {content}
        </button>
      ) : (
        <div className={rowClass}>{content}</div>
      )}
      {item.draft && !item.archived ? <ProvenanceFooter kind={kind} definitionId={item.id} card="draft" /> : null}
    </li>
  );
}

function Group({ heading, headingId, kind, items, showArchived, onToggle, selectedId, onSelect }: {
  heading: string;
  kind: "agent" | "workflow";
  headingId: string;
  items: readonly CreatedItem[];
  showArchived: boolean;
  onToggle: () => void;
  selectedId: string | null;
  onSelect?: (item: CreatedItem) => void;
}) {
  const archivedCount = items.filter((i) => i.archived).length;
  const visible = showArchived ? items : items.filter((i) => !i.archived);
  return (
    <section aria-labelledby={headingId} className="min-w-0">
      <h2 id={headingId} className="mb-1 px-2 text-caption font-medium uppercase tracking-wide text-muted-foreground">{heading}</h2>
      {visible.length === 0 ? (
        <p className="px-2 text-label text-muted-foreground">Nothing created yet.</p>
      ) : (
        <ul className="space-y-0.5">
          {visible.map((item) => (
            <Row key={item.id} item={item} kind={kind} selected={item.id === selectedId} onSelect={onSelect ? () => onSelect(item) : undefined} />
          ))}
        </ul>
      )}
      {archivedCount > 0 ? (
        <button type="button" onClick={onToggle} aria-pressed={showArchived} data-testid={`${headingId}-toggle-archived`} className="mt-1 px-2 text-caption text-muted-foreground hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring">
          {showArchived ? "Hide archived" : "Show archived"} ({archivedCount})
        </button>
      ) : null}
    </section>
  );
}

/** The left column: "+ New" (an agent or a workflow), then what the Architect page created, each by name with `id · vN` beneath; archived ones are behind a toggle. */
export function CreatedList({ agents, workflows, selectedAgentId, onNew, onSelectAgent }: CreatedListProps) {
  const [showArchived, setShowArchived] = useState(false);
  useEffect(() => setShowArchived(readShowArchived()), []);
  const toggle = () => {
    const next = !showArchived;
    setShowArchived(next);
    try {
      window.localStorage.setItem(ARCHITECT_SHOW_ARCHIVED_KEY, String(next));
    } catch {
      /* storage unavailable: the choice lasts for this page view */
    }
  };
  return (
    <div data-testid="architect-created" className="flex min-w-0 flex-col gap-4">
      <DropdownMenu>
        <DropdownMenuTrigger asChild>
          <Button variant="primary" data-testid="architect-new">
            <Plus strokeWidth={ICON_STROKE_WIDTH} aria-hidden="true" />
            New
          </Button>
        </DropdownMenuTrigger>
        <DropdownMenuContent align="start">
          <DropdownMenuItem data-testid="architect-new-agent" onSelect={() => onNew("agent")}>Agent</DropdownMenuItem>
          <DropdownMenuItem data-testid="architect-new-workflow" onSelect={() => onNew("workflow")}>Workflow</DropdownMenuItem>
        </DropdownMenuContent>
      </DropdownMenu>
      <Group heading="Agents" headingId="architect-created-agents" kind="agent" items={agents} showArchived={showArchived} onToggle={toggle} selectedId={selectedAgentId} onSelect={onSelectAgent} />
      <Group heading="Workflows" headingId="architect-created-workflows" kind="workflow" items={workflows} showArchived={showArchived} onToggle={toggle} selectedId={null} />
    </div>
  );
}
