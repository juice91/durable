"use client";

import React, { useEffect } from "react";
import { X } from "lucide-react";
import { IconButton } from "@/components/ui/IconButton";
import { CopyButton } from "@/components/ui/CopyButton";
import { ICON_STROKE_WIDTH } from "@/lib/icons";
import type { ToolDefinition } from "@/lib/tools";
import type { WorkflowStep } from "@/lib/workflows";
import { BranchCases, branchCasesText } from "./BranchCases";
import { stepTypeLabel } from "./WorkflowGraph";

export interface StepAgentInfo {
  id: string;
  name: string;
  /** The version the step runs: its pinned one, else the latest published; null when unknown. */
  version: number | null;
  pinned: boolean;
  model: string | null;
}

export interface StepDefinitionPanelProps {
  step: WorkflowStep;
  /** The tool's declaration from `/api/v1/tools`; null when the catalog does not list it. */
  tool?: ToolDefinition | null;
  agent?: StepAgentInfo | null;
  /** Where the step goes next (a branch lists its cases instead). */
  next?: string[];
  /**
   * The AI 註解 (explanation) cached for this step. Hook: the page passes it once the cached
   * explanations (`lib/workflowExplanations.ts`) are on main; null shows no section.
   */
  explanation?: string | null;
  onClose: () => void;
  className?: string;
}

/** The step's full definition as pretty JSON. */
export function stepJson(step: WorkflowStep): string {
  return JSON.stringify(step, null, 2);
}

/**
 * The definition view's step card: what the selected step is (its JSON, the tool declaration or the
 * agent, where it goes next, the AI 註解). The run view's `StepIoPanel` shows the same step's run-time
 * input and output; this one shows the definition. Escape closes it.
 */
export function StepDefinitionPanel({ step, tool, agent, next = [], explanation, onClose, className }: StepDefinitionPanelProps) {
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape" && !event.defaultPrevented) onClose();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [onClose]);
  const json = stepJson(step);
  return (
    <section aria-label={`Step ${step.id} definition`} data-testid="step-definition" className={className ?? "min-w-0 space-y-3"}>
      <div className="flex min-w-0 items-center justify-between gap-2">
        <h3 className="min-w-0 truncate text-label font-semibold text-foreground">
          <span className="font-mono">{step.id}</span> <span className="font-normal text-muted-foreground">{stepTypeLabel(step.type)}</span>
        </h3>
        <IconButton label="Close step details" title="Close step details" size="sm" tooltip={false} data-testid="step-definition-close" icon={<X strokeWidth={ICON_STROKE_WIDTH} aria-hidden="true" />} onClick={onClose} />
      </div>
      {tool ? (
        <div data-testid="step-definition-tool">
          <h4 className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">Tool</h4>
          <p className="text-sm font-medium text-foreground">{tool.title} <span className="font-mono text-caption font-normal text-muted-foreground">{tool.id}</span></p>
          <p className="whitespace-pre-wrap break-words text-caption text-muted-foreground">{tool.description}</p>
        </div>
      ) : null}
      {agent ? (
        <div data-testid="step-definition-agent">
          <h4 className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">Agent</h4>
          <p className="text-sm font-medium text-foreground">{agent.name}</p>
          <p className="text-caption text-muted-foreground">
            {agent.version !== null ? `v${agent.version}${agent.pinned ? "" : " (latest)"}` : "latest"}
            {agent.model ? ` · ${agent.model}` : ""}
          </p>
        </div>
      ) : null}
      {step.type === "branch" ? (
        <div>
          <h4 className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">Cases</h4>
          <div data-copy-text={branchCasesText(step)} className="mt-1"><BranchCases step={step} /></div>
        </div>
      ) : next.length > 0 ? (
        <p data-testid="step-definition-next" className="text-caption text-muted-foreground">Next: <span className="font-mono text-foreground">{next.join(", ")}</span></p>
      ) : null}
      {explanation ? (
        <div data-testid="step-definition-explanation">
          <h4 className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">AI 註解</h4>
          <p className="whitespace-pre-wrap break-words text-caption text-foreground">{explanation}</p>
        </div>
      ) : null}
      <div>
        <div className="flex items-center justify-between gap-2">
          <h4 className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">Definition</h4>
          <CopyButton text={json} label="step definition" data-testid="step-definition-copy" />
        </div>
        <pre data-testid="step-definition-json" tabIndex={0} className="mt-1 max-h-72 cursor-text select-text overflow-auto whitespace-pre-wrap break-words rounded-md bg-muted p-2 font-mono text-xs text-foreground outline-none focus-visible:ring-2 focus-visible:ring-ring">{json}</pre>
      </div>
    </section>
  );
}
