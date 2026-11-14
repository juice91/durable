"use client";

import React from "react";
import Link from "next/link";
import { ConfirmDialog } from "@/components/shared/ConfirmDialog";
import { describeUsage, type AgentSpecsStatus, type AgentUsage } from "@/lib/agentUsage";
import { referenceLabel, type KnowledgeBase, type KnowledgeBaseReference } from "@/lib/knowledge";
import { routes } from "@/lib/routes";

export interface KnowledgeDeleteConfirmProps {
  kb: KnowledgeBase;
  /** Agents whose draft or latest version attaches it (a fresh client-side check). */
  usage: AgentUsage[];
  usageStatus: AgentSpecsStatus;
  usageRefreshing?: boolean;
  usagePartial?: boolean;
  /** The API's 409 list (agents whose latest published version, or draft only, attaches it): delete is then forced. */
  blockedBy: KnowledgeBaseReference[] | null;
  busy?: boolean;
  error?: string | null;
  onConfirm: () => void;
  onCancel: () => void;
}

/** "Delete {name}?" listing the agents that attach it; their runs then get a "no longer exists" tool error. */
export function KnowledgeDeleteConfirm({ kb, usage, usageStatus, usageRefreshing = false, usagePartial = false, blockedBy, busy, error, onConfirm, onCancel }: KnowledgeDeleteConfirmProps) {
  let references: React.ReactNode;
  if (blockedBy && blockedBy.length > 0) {
    references = (
      <>
        <p>
          {blockedBy.some((item) => item.draft || item.version === null)
            ? blockedBy.length === 1
              ? "This agent attaches it:"
              : `These ${blockedBy.length} agents attach it (their latest published version, or their draft):`
            : blockedBy.length === 1
              ? "This agent's latest published version attaches it:"
              : `These ${blockedBy.length} agents' latest published version attaches it:`}
        </p>
        <ul className="mt-2 space-y-1" data-testid="knowledge-delete-reference-list">
          {blockedBy.map((item) => (
            <li key={item.agent_id} data-testid="knowledge-delete-reference" className="flex min-w-0 flex-wrap items-baseline gap-x-2">
              <Link href={routes.agentEditor(item.agent_id)} className="font-medium text-foreground underline-offset-4 hover:underline">{item.agent_name}</Link>{" "}
              <span className="text-caption" data-draft={item.draft || item.version === null ? "true" : undefined}>{referenceLabel(item)}</span>
            </li>
          ))}
        </ul>
        <p className="mt-2">Delete anyway? Their runs will get a &quot;knowledge base no longer exists&quot; tool error until they are changed.</p>
      </>
    );
  } else if (usageStatus === "loading") {
    references = <p>Checking which agents attach it…</p>;
  } else if (usageStatus === "error") {
    references = <p>Could not check which agents attach it. If an agent&apos;s published version does, you will be asked again.</p>;
  } else if (usage.length === 0) {
    references = <p>{usagePartial ? "No agent draft or readable published version attaches it." : "No agent attaches it."}</p>;
  } else {
    references = (
      <>
        <p>{usage.length === 1 ? "This agent attaches" : `These ${usage.length} agents attach`} it and will get a &quot;knowledge base no longer exists&quot; tool error until changed:</p>
        <ul className="mt-2 space-y-1" data-testid="knowledge-delete-reference-list">
          {usage.map((item) => (
            <li key={item.agentId} data-testid="knowledge-delete-reference" className="flex min-w-0 flex-wrap items-baseline gap-x-2">
              <Link href={routes.agentEditor(item.agentId)} className="font-medium text-foreground underline-offset-4 hover:underline">{item.agentName}</Link>
              <span className="text-caption">{describeUsage(item)}</span>
            </li>
          ))}
        </ul>
        {usagePartial ? <p className="mt-2">Some published versions could not be read; other agents may also attach it.</p> : null}
      </>
    );
  }
  const documents = kb.document_count ?? 0;
  return (
    <ConfirmDialog
      title={`Delete ${kb.name}?`}
      description={
        <div className="space-y-2">
          <p>
            Its {documents} document{documents === 1 ? "" : "s"} and every section are deleted. Ingest runs stay in Traces.
          </p>
          <div data-testid="knowledge-delete-references" data-status={blockedBy ? "blocked" : usageStatus} aria-busy={usageRefreshing || undefined}>
            {references}
            {usageRefreshing && usageStatus !== "loading" && !blockedBy ? <p className="mt-2">Re-checking…</p> : null}
          </div>
        </div>
      }
      confirmLabel={blockedBy ? "Delete anyway" : "Delete knowledge base"}
      destructive
      confirmDisabled={!blockedBy && (usageStatus === "loading" || usageRefreshing)}
      busy={busy}
      error={error}
      onConfirm={onConfirm}
      onCancel={onCancel}
    />
  );
}
