"use client";

import React, { useRef, useState } from "react";
import { MoreHorizontal } from "lucide-react";
import { AgentArchiveBlocked, api, ApiError, type Agent, type ReferencingWorkflow } from "@/lib/api";
import { ICON_STROKE_WIDTH } from "@/lib/icons";
import { routes } from "@/lib/routes";
import { ConfirmDialog } from "@/components/shared/ConfirmDialog";
import { Button } from "@/components/ui/Button";
import { IconButton } from "@/components/ui/IconButton";
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuTrigger } from "@/components/ui/DropdownMenu";
import { toast } from "@/components/ui/Toaster";
import Link from "next/link";

export const ARCHIVE_DESCRIPTION =
  "It leaves the agents list, and new chats, runs and publishes are refused until you unarchive it. Its conversations, runs, traces and memory stay readable.";

function failure(error: unknown, action: string): string {
  if (error instanceof ApiError && error.status === 404) return "This agent no longer exists.";
  return error instanceof ApiError && error.message ? `Could not ${action} the agent: ${error.message}` : `Could not ${action} the agent. Try again.`;
}

/** The agent page header's overflow menu: Archive (behind a ConfirmDialog) or Unarchive. */
export function AgentArchiveMenu({ agent, onChanged }: { agent: Agent; onChanged: (agent: Agent) => void }) {
  const [confirming, setConfirming] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // Published workflows that run this agent (the API's 409): shown, with "Archive anyway".
  const [blockers, setBlockers] = useState<ReferencingWorkflow[] | null>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);

  const archive = async () => {
    setBusy(true);
    setError(null);
    try {
      onChanged(await api.agents.archive(agent.id, { force: blockers !== null }));
      setConfirming(false);
      setBlockers(null);
      toast.success(`Archived ${agent.name}`);
    } catch (err) {
      if (err instanceof AgentArchiveBlocked) setBlockers(err.workflows);
      else setError(failure(err, "archive"));
    } finally {
      setBusy(false);
    }
  };
  const close = () => {
    setConfirming(false);
    setError(null);
    setBlockers(null);
  };

  return (
    <>
      <DropdownMenu>
        <DropdownMenuTrigger asChild>
          <IconButton
            ref={triggerRef}
            label="More actions"
            size="sm"
            icon={<MoreHorizontal strokeWidth={ICON_STROKE_WIDTH} aria-hidden="true" />}
            data-testid="agent-more-actions"
          />
        </DropdownMenuTrigger>
        <DropdownMenuContent align="end">
          {agent.archived ? (
            <DropdownMenuItem data-testid="agent-unarchive" onSelect={() => void unarchiveAgent(agent, onChanged)}>
              Unarchive agent
            </DropdownMenuItem>
          ) : (
            <DropdownMenuItem
              destructive
              data-testid="agent-archive"
              onSelect={() => {
                setError(null);
                setConfirming(true);
              }}
            >
              Archive agent
            </DropdownMenuItem>
          )}
        </DropdownMenuContent>
      </DropdownMenu>
      {confirming ? (
        <ConfirmDialog
          title={`Archive ${agent.name}?`}
          description={
            blockers ? (
              <div className="space-y-2" data-testid="agent-archive-workflows">
                <p>
                  {blockers.length === 1 ? "A published workflow runs" : `${blockers.length} published workflows run`} this agent as a
                  step. Archiving it makes their runs fail before they start:
                </p>
                <ul className="list-disc space-y-1 pl-5">
                  {blockers.map((w) => (
                    <li key={w.workflow_id} className="min-w-0 break-words">
                      <Link href={routes.workflow(w.workflow_id)} className="underline underline-offset-2">
                        {w.workflow_name}
                      </Link>{" "}
                      <span className="text-muted-foreground">
                        ({w.versions.length === 1 ? `version ${w.versions[0]}` : `versions ${w.versions.join(", ")}`})
                      </span>
                    </li>
                  ))}
                </ul>
                <p>Remove the step from those workflows first, or archive anyway.</p>
              </div>
            ) : (
              <p>{ARCHIVE_DESCRIPTION}</p>
            )
          }
          confirmLabel={blockers ? "Archive anyway" : "Archive agent"}
          destructive
          busy={busy}
          error={error}
          onConfirm={() => void archive()}
          onCancel={close}
          fallbackFocus={() => triggerRef.current}
        />
      ) : null}
    </>
  );
}

/** Unarchive (no confirmation: it only makes the agent usable again); failures are toasts. */
export async function unarchiveAgent(agent: Pick<Agent, "id" | "name">, onChanged: (agent: Agent) => void): Promise<boolean> {
  try {
    onChanged(await api.agents.unarchive(agent.id));
    toast.success(`Unarchived ${agent.name}`);
    return true;
  } catch (err) {
    toast.error(failure(err, "unarchive"));
    return false;
  }
}

/** Shown under the header of an archived agent's page. */
export function ArchivedBanner({ agent, onChanged }: { agent: Agent; onChanged: (agent: Agent) => void }) {
  const [busy, setBusy] = useState(false);
  return (
    <div
      role="status"
      data-testid="agent-archived-banner"
      className="flex min-w-0 flex-wrap items-center gap-x-3 gap-y-2 border-b border-border bg-muted px-4 py-2 text-label text-muted-foreground md:px-6"
    >
      <span className="min-w-0 flex-1">
        This agent is archived: new chats, runs and publishes are refused. Its history stays readable.{" "}
        <Link href={routes.agents({ archived: true })} className="underline underline-offset-2">
          Archived agents
        </Link>
      </span>
      <Button
        size="sm"
        disabled={busy}
        onClick={async () => {
          setBusy(true);
          await unarchiveAgent(agent, onChanged);
          setBusy(false);
        }}
      >
        Unarchive
      </Button>
    </div>
  );
}
