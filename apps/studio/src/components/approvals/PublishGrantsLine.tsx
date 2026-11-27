"use client";

import React, { useEffect, useState } from "react";
import { getApproval } from "@/lib/approvals";
import type { PublishGrants } from "@/lib/agentGrants";

/** The approval tools that publish a definition: the Architect's, and the control plane's `agent.publish` / `workflow.publish`. */
const PUBLISH_TOOLS = [
  "architect.publish_agent", "architect.publish_workflow", "architect.restore_agent_version", "architect.restore_workflow_version",
  "control.agent_publish", "control.workflow_publish",
];

export function isPublishTool(toolName: string): boolean {
  const name = toolName.replace(/__/g, ".");
  return PUBLISH_TOOLS.some((tool) => name === tool || name.endsWith(`.${tool}`));
}

/**
 * The publish card's grant line (docs/iam.md, "Agent and workflow grants"): "This version newly uses MCP server `x`, secret `y`.
 * Approving grants them." The API lists what the draft newly uses and no grant covers yet on the approval it serves
 * (`grants`); an admin's approval is the grant, so a caller who may not approve is told an admin must, and the card disables
 * Approve (`onBlocked`). Nothing is shown when nothing is new, the engine keeps no grants, or the line cannot be read.
 */
export function PublishGrantsLine({ runId, toolCallId, onBlocked }: { runId: string; toolCallId: string; onBlocked?: (blocked: boolean) => void }) {
  const [grants, setGrants] = useState<PublishGrants | null>(null);
  useEffect(() => {
    const controller = new AbortController();
    getApproval(runId, "tool", toolCallId, { signal: controller.signal })
      .then((approval) => {
        const found = (approval as { grants?: PublishGrants | null } | null)?.grants ?? null;
        setGrants(found && found.message ? found : null);
        onBlocked?.(Boolean(found && found.message && !found.can_approve));
      })
      .catch(() => undefined); // best effort: the card works without the line
    return () => controller.abort();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [runId, toolCallId]);
  if (!grants) return null;
  return (
    <div data-testid={`approval-grants-${toolCallId}`} className="mb-2 rounded-md border border-border bg-fill-hover p-2 text-sm text-foreground">
      <p>{grants.message}</p>
      {!grants.can_approve ? (
        <p role="status" className="mt-1 text-caption text-muted-foreground">
          An admin must approve this publish.
        </p>
      ) : null}
    </div>
  );
}
