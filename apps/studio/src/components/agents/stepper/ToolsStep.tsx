"use client";

import React, { useEffect, useState } from "react";
import { listTools, type ToolDefinition } from "@/lib/tools";
import type { McpToolRef } from "@/lib/api";
import { approvalProblems, approvalsOf, builtinApprovalSuggestion, mcpApprovalRef, mcpApprovalSuggestion, prunedApprovals } from "../agentCapabilities";
import { McpToolPicker } from "../McpToolPicker";
import { stepForErrorMessage, stepStatus } from "../specSections";
import { APPROVAL_TIMEOUT_ID, ApprovalSettings, ApprovalToggle, staleApprovalId, toggledApprovals } from "./ApprovalToggles";
import type { EditorStepDefinition, MissingField, StepProps } from "./types";

/**
 * Step 3: built-in tools and MCP tools. A selected tool's row carries its "Require approval" toggle
 * (AC.13: `spec.approvals`, edited through `onSpecChange`); the approval timeout follows the lists.
 * Unselecting a tool also drops its approval entry, so a save never names a tool the agent no
 * longer has.
 */
export function ToolsStep({ draft, spec, onChange, onSpecChange }: StepProps) {
  const [tools, setTools] = useState<ToolDefinition[]>([]);
  const [toolsError, setToolsError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    listTools()
      .then((items) => {
        if (!cancelled) setTools(items);
      })
      .catch((error: unknown) => {
        if (!cancelled) setToolsError(error instanceof Error ? error.message : "Failed to load tools");
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const approvals = approvalsOf(spec);
  const setToolLists = (nextTools: string[], mcpTools: McpToolRef[]) => {
    onChange({ tools: nextTools, mcpTools });
    // Only when an entry goes, so an untouched agent's `approvals` (and its hash) is never rewritten.
    const pruned = prunedApprovals(approvals, nextTools, mcpTools);
    if (approvals && pruned?.tools.length !== approvals.tools.length) onSpecChange({ approvals: pruned });
  };
  const gated = new Set(approvals?.tools ?? []);
  const setApproval = (ref: string, on: boolean) => onSpecChange({ approvals: toggledApprovals(approvals, ref, on) });
  const approvalToggle = (ref: string, suggestion: string | null) => (
    <ApprovalToggle toolRef={ref} checked={gated.has(ref)} suggestion={suggestion} onChange={(on) => setApproval(ref, on)} />
  );
  const toggle = (id: string) => setToolLists(draft.tools.includes(id) ? draft.tools.filter((t) => t !== id) : [...draft.tools, id], draft.mcpTools);

  return (
    <div className="space-y-6">
      <fieldset className="min-w-0">
        <legend className="mb-2 text-label font-medium text-foreground">Built-in tools</legend>
        <div className="flex min-w-0 flex-wrap gap-2">
          {tools.map((tool) => (
            <div key={tool.id} data-testid="builtin-tool-row" data-tool-id={tool.id} className="inline-flex min-w-0 max-w-full flex-wrap items-center gap-x-3 gap-y-1 rounded-md border border-border bg-surface">
              <label
                className={`inline-flex min-w-0 max-w-full flex-wrap items-center gap-x-2 rounded-md px-3 py-2 ${tool.allowed ? "cursor-pointer hover:bg-fill-hover" : "cursor-not-allowed opacity-60"}`}
              >
                <input
                  type="checkbox"
                  aria-label={`${tool.title} (${tool.id})`}
                  className="size-4 accent-primary"
                  checked={draft.tools.includes(tool.id)}
                  disabled={!tool.allowed}
                  onChange={() => toggle(tool.id)}
                />
                <span className="min-w-0 break-words text-label text-foreground">
                  {tool.title} ({tool.id})
                </span>
                {!tool.allowed ? <span className="text-caption text-muted-foreground">Disabled by platform policy</span> : null}
              </label>
              {draft.tools.includes(tool.id) ? <span className="min-w-0 px-3 pb-2 sm:pb-0 sm:pl-0">{approvalToggle(tool.id, builtinApprovalSuggestion(tool))}</span> : null}
            </div>
          ))}
        </div>
        {toolsError ? (
          <p role="alert" className="mt-2 text-caption text-destructive-text">
            {toolsError}
          </p>
        ) : null}
      </fieldset>

      <McpToolPicker
        value={draft.mcpTools}
        onChange={(mcpTools) => setToolLists(draft.tools, mcpTools)}
        renderSelectedExtra={(ref, annotations) => approvalToggle(mcpApprovalRef(ref), mcpApprovalSuggestion(annotations))}
      />

      <ApprovalSettings spec={spec} approvals={approvals} onChange={(next) => onSpecChange({ approvals: next })} />
    </div>
  );
}

export const toolsStep: EditorStepDefinition = {
  id: "tools",
  label: "Tools",
  Component: ToolsStep,
  status: (spec) => stepStatus("tools", spec)!,
  ownsError: (message) => stepForErrorMessage(message) === "tools",
  missing: (_draft, spec): MissingField[] =>
    approvalProblems(spec).map((problem) => ({
      step: "tools",
      fieldId: problem.field === "timeout_hours" ? APPROVAL_TIMEOUT_ID : problem.field.startsWith("stale-") ? staleApprovalId(Number(problem.field.slice(6))) : "approvals-problem",
      message: problem.message,
    })),
};
