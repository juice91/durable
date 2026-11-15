"use client";

/**
 * The Tools step's approvals (AC.13).
 * - `ApprovalToggle` is the "Require approval" checkbox in each selected tool's own row: built-in
 *   rows in ToolsStep, MCP rows through McpToolPicker's `renderSelectedExtra`. It carries a
 *   suggestion mark only where there is evidence of a side effect.
 * - `ApprovalSettings` holds the timeout (once any tool is gated) and any entries that name no tool
 *   of the agent.
 * Together they write `spec.approvals`, which is left out of the spec while no tool is gated. A
 * suggestion never changes the spec; only a tick does. Tokens only.
 */
import React from "react";
import type { AgentSpec } from "@/lib/api";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import {
  approvalProblems,
  approvalsValue,
  DEFAULT_APPROVAL_TIMEOUT_HOURS,
  MAX_APPROVAL_TIMEOUT_HOURS,
  mcpApprovalRef,
  type ApprovalPolicyValue,
} from "../agentCapabilities";
import { FieldProblem, NumberField } from "./knowledgeMemoryFields";

/** Element id of the approval timeout field. */
export const APPROVAL_TIMEOUT_ID = "approvals-timeout_hours";
/** Element id of an approval toggle (`approval-<ref>`; refs are tool ids or `server/tool`). */
export const approvalToggleId = (ref: string) => `approval-${ref}`;
/** Element id of the Remove button of a stale approval entry. */
export const staleApprovalId = (index: number) => `approvals-stale-${index}`;

/** `approvals` with `ref` gated (`on`) or not; undefined once nothing is gated. */
export function toggledApprovals(approvals: ApprovalPolicyValue | null, ref: string, on: boolean): ApprovalPolicyValue | undefined {
  const tools = (approvals?.tools ?? []).filter((t) => t !== ref);
  return approvalsValue({ ...(approvals ?? {}), tools: on ? [...tools, ref] : tools });
}

/** One tool row's "Require approval" checkbox, with the suggestion mark when there is one. */
export function ApprovalToggle({
  toolRef,
  checked,
  suggestion,
  onChange,
}: {
  toolRef: string;
  checked: boolean;
  suggestion: string | null;
  onChange: (checked: boolean) => void;
}) {
  return (
    <span data-testid="approval-toggle" data-tool-ref={toolRef} className="inline-flex min-w-0 flex-wrap items-center gap-2">
      <label className="inline-flex shrink-0 cursor-pointer items-center gap-1.5 text-caption text-foreground">
        <input
          id={approvalToggleId(toolRef)}
          type="checkbox"
          aria-label={`Require approval for ${toolRef}`}
          className="size-4 accent-primary"
          checked={checked}
          onChange={(e) => onChange(e.target.checked)}
        />
        <span aria-hidden="true">Require approval</span>
      </label>
      {suggestion ? (
        <Badge variant="warning" data-testid="approval-suggested" title={suggestion}>
          Suggested: {suggestion.toLowerCase()}
        </Badge>
      ) : null}
    </span>
  );
}

/** The timeout (shown once any tool is gated) and entries that name no tool of the agent. */
export function ApprovalSettings({
  spec,
  approvals,
  onChange,
}: {
  spec: AgentSpec;
  approvals: ApprovalPolicyValue | null;
  onChange: (next: ApprovalPolicyValue | undefined) => void;
}) {
  if (!approvals || approvals.tools.length === 0) return null;
  const known = new Set([...(spec.tools ?? []), ...(spec.mcp_tools ?? []).map(mcpApprovalRef)]);
  const stale = approvals.tools.map((ref, index) => ({ ref, index })).filter(({ ref }) => !known.has(ref));
  const problems = approvalProblems(spec);
  return (
    <div data-testid="approval-settings" className="min-w-0 space-y-3">
      {stale.length > 0 ? (
        <ul className="min-w-0 space-y-2">
          {stale.map(({ ref, index }) => (
            <li key={`stale:${ref}`} data-testid="approval-stale" className="flex min-w-0 flex-wrap items-center gap-x-3 gap-y-1">
              <span className="min-w-0 flex-1 break-words text-label text-destructive-text">
                <span className="font-mono">{ref}</span> requires approval but is not a tool of this agent
              </span>
              <Button id={staleApprovalId(index)} size="sm" onClick={() => onChange(toggledApprovals(approvals, ref, false))}>
                Remove
              </Button>
            </li>
          ))}
        </ul>
      ) : null}
      <NumberField
        id={APPROVAL_TIMEOUT_ID}
        label={`Approval timeout (hours, 1–${MAX_APPROVAL_TIMEOUT_HOURS})`}
        value={approvals.timeout_hours}
        min={1}
        max={MAX_APPROVAL_TIMEOUT_HOURS}
        placeholder={`${DEFAULT_APPROVAL_TIMEOUT_HOURS} (default)`}
        hint="A call that requires approval pauses the run until someone decides. With no decision in time it counts as denied and is not run; a denial is returned to the model, which carries on."
        onChange={(value) => onChange(approvalsValue({ tools: approvals.tools, timeout_hours: value }))}
        problem={problems.find((p) => p.field === "timeout_hours")?.message ?? null}
      />
      <FieldProblem id="approvals-problem" message={problems.find((p) => p.field === "tools")?.message} />
    </div>
  );
}
