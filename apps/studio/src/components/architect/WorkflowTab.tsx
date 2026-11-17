"use client";

import React from "react";
import type { ArchitectOptions, CreatedItem } from "@/lib/architect";
import { Skeleton } from "@/components/ui/Skeleton";
import { WorkflowForm } from "./WorkflowForm";

export interface WorkflowTabProps {
  options: ArchitectOptions | null;
  /** GET /architect `agents`: the Architect-made agents a step may use. */
  agents: readonly CreatedItem[];
  /** Call after a workflow was saved, so the created list refreshes. */
  onSaved: () => void;
}

/** The Workflow tab: the workflow form of the Architect page. */
export function WorkflowTab({ options, agents, onSaved }: WorkflowTabProps) {
  if (!options) return <div className="space-y-3" aria-busy="true"><Skeleton className="h-8 w-64" /><Skeleton className="h-48 w-full" /></div>;
  return <WorkflowForm options={options} agents={agents} onSaved={() => onSaved()} />;
}
