"use client";

import React, { useMemo, useState } from "react";
import { agentUsageByKey, useAgentLatestSpecs } from "@/lib/agentUsage";
import { deleteKnowledgeBase, KnowledgeBaseInUseError, knowledgeBaseIdsOf, type KnowledgeBase, type KnowledgeBaseReference } from "@/lib/knowledge";
import { ApiError } from "@/lib/api";
import { KnowledgeDeleteConfirm } from "./KnowledgeDeleteConfirm";

export interface KnowledgeDeleteFlowProps {
  kb: KnowledgeBase;
  /** Deleted (or already gone). */
  onDeleted: () => void;
  onCancel: () => void;
}

/**
 * The delete confirm with its reference check. Mounted only while open, so the agents scan (every
 * agent's draft and latest version) runs when it is needed and is always fresh.
 *
 * The scan only INFORMS: it cannot see archived agents (GET /agents leaves them out), a partial scan
 * misses unreadable versions, and an agent can publish between the scan and the click. So the first
 * DELETE never forces; the API decides. Only after its 409 list is shown does the button become
 * "Delete anyway", and only that click sends `force=true`.
 */
export function KnowledgeDeleteFlow({ kb, onDeleted, onCancel }: KnowledgeDeleteFlowProps) {
  const usageState = useAgentLatestSpecs();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [blockedBy, setBlockedBy] = useState<KnowledgeBaseReference[] | null>(null);

  const usage = useMemo(
    () => (usageState.status === "ready" ? agentUsageByKey(usageState.agents, usageState.latestSpecs, knowledgeBaseIdsOf).get(kb.id) ?? [] : []),
    [usageState.status, usageState.agents, usageState.latestSpecs, kb.id],
  );

  const confirm = async () => {
    if (busy) return;
    setBusy(true);
    setError(null);
    // Force only after the API's own reference list (409) has been shown.
    const force = blockedBy !== null;
    try {
      await deleteKnowledgeBase(kb.id, { force });
      onDeleted();
    } catch (reason: unknown) {
      if (reason instanceof KnowledgeBaseInUseError) {
        setBlockedBy(reason.agents);
      } else if (reason instanceof ApiError && reason.status === 404) {
        onDeleted();
      } else {
        setError(reason instanceof Error ? reason.message : "The knowledge base could not be deleted");
      }
      setBusy(false);
    }
  };

  return (
    <KnowledgeDeleteConfirm
      kb={kb}
      usage={usage}
      usageStatus={usageState.status}
      usageRefreshing={usageState.refreshing}
      usagePartial={usageState.partial}
      blockedBy={blockedBy}
      busy={busy}
      error={error}
      onConfirm={() => void confirm()}
      onCancel={onCancel}
    />
  );
}
