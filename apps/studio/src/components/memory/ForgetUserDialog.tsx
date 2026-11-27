"use client";

import { useCan } from "@/lib/permissions";
import React, { useId, useState } from "react";
import { forgetResource, type ForgetResult } from "@/lib/memory";
import { Button } from "@/components/ui/Button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/Dialog";

export type ForgetScope = "agent" | "all";

export interface ForgetUserDialogProps {
  agentId: string;
  agentName: string;
  resourceId: string;
  onDone: (result: ForgetResult) => void;
  onCancel: () => void;
}

/** One line for the toast: what a forget removed. */
export function forgetSummary(result: ForgetResult): string {
  const parts = [
    `${result.working_memories_deleted} working ${result.working_memories_deleted === 1 ? "memory" : "memories"}`,
    `${result.message_embeddings_deleted} recall ${result.message_embeddings_deleted === 1 ? "entry" : "entries"}`,
    `${result.conversations_deleted.length} ${result.conversations_deleted.length === 1 ? "conversation" : "conversations"}`,
  ];
  return `Forgot ${result.resource_id}: ${parts.join(", ")} deleted`;
}

/**
 * "Forget user" (decision 2026-11-14): a scope choice, this agent (`DELETE
 * /agents/{id}/memory/resources/{rid}`) or all agents (`DELETE /memory/resources/{rid}`), and
 * `include_conversations=true` by default, which the dialog says: the user's conversations and
 * their transcripts go too, unless "Keep their conversations" is checked. A busy 409 (a turn of the
 * user is running) deletes nothing and is shown here.
 */
export function ForgetUserDialog({ agentId, agentName, resourceId, onDone, onCancel }: ForgetUserDialogProps) {
  const [scope, setScope] = useState<ForgetScope>("agent");
  // Forgetting across every agent is a tenant-wide change.
  const mayForgetEverywhere = useCan("tenant", null, "manage_settings");
  const [keepConversations, setKeepConversations] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const name = useId();
  const keepId = useId();

  const confirm = async () => {
    setBusy(true);
    setError(null);
    try {
      const result = await forgetResource(resourceId, { agentId: scope === "agent" ? agentId : null, includeConversations: !keepConversations });
      onDone(result);
    } catch (reason) {
      const message = reason instanceof Error && reason.message ? reason.message : "Failed to forget the user.";
      // A busy 409 ("a turn is still queued or running …; nothing was deleted") reads as it is.
      setError(message);
      setBusy(false);
    }
  };

  const where = scope === "agent" ? `${agentName}'s` : "every agent's";
  return (
    <Dialog
      open
      onOpenChange={(open) => {
        if (!open && !busy) onCancel();
      }}
    >
      <DialogContent data-testid="memory-forget-dialog" showClose={!busy} onEscapeKeyDown={(event) => busy && event.preventDefault()} onInteractOutside={(event) => event.preventDefault()}>
        <DialogHeader>
          <DialogTitle>Forget {resourceId}?</DialogTitle>
          <DialogDescription>
            Deletes {where} working memory and recall index for this user
            {keepConversations
              ? "."
              : `, and also deletes this user's conversations${scope === "agent" ? ` with ${agentName}` : ""} and their transcripts (their runs stay in Traces with the text removed).`}{" "}
            This cannot be undone.
          </DialogDescription>
        </DialogHeader>
        <fieldset className="min-w-0 space-y-1.5">
          <legend className="mb-1 text-label font-medium text-foreground">Forget for</legend>
          <label className="flex items-center gap-2 text-label text-foreground">
            <input type="radio" name={name} value="agent" checked={scope === "agent"} onChange={() => setScope("agent")} disabled={busy} data-testid="memory-forget-scope-agent" />
            This agent ({agentName})
          </label>
          {mayForgetEverywhere ? (
            <label className="flex items-center gap-2 text-label text-foreground">
              <input type="radio" name={name} value="all" checked={scope === "all"} onChange={() => setScope("all")} disabled={busy} data-testid="memory-forget-scope-all" />
              All agents
            </label>
          ) : null}
        </fieldset>
        <div className="flex items-start gap-2">
          <input
            id={keepId}
            type="checkbox"
            className="mt-0.5"
            checked={keepConversations}
            onChange={(event) => setKeepConversations(event.target.checked)}
            disabled={busy}
            data-testid="memory-forget-keep-conversations"
          />
          <label htmlFor={keepId} className="text-label text-foreground">
            Keep their conversations
            <span className="block text-caption text-muted-foreground">Only the cross-conversation memory is deleted; transcripts, observations and per-conversation notes stay.</span>
          </label>
        </div>
        {error ? (
          <p role="alert" data-testid="memory-forget-error" className="rounded-md border border-destructive/30 bg-destructive/10 p-3 text-body text-destructive-text">
            {error}
          </p>
        ) : null}
        <DialogFooter>
          <Button onClick={onCancel} disabled={busy}>
            Cancel
          </Button>
          <Button variant="destructive" onClick={() => void confirm()} disabled={busy} data-testid="memory-forget-confirm">
            {busy ? "Forgetting…" : "Forget user"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
