"use client";

import React from "react";
import type { ArchitectOptions } from "@/lib/architect";

export interface WorkflowTabProps {
  options: ArchitectOptions | null;
  /** Call after a workflow was saved, so the created list refreshes. */
  onSaved: () => void;
}

/** Placeholder for the Workflow tab; Task 8 mounts the workflow form here. */
export function WorkflowTab(_props: WorkflowTabProps) {
  return <p data-testid="architect-workflow-stub" className="text-body text-muted-foreground">The workflow form is coming here.</p>;
}
