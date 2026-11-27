"use client";

import React, { useRef, useState } from "react";
import Link from "next/link";
import { MoreHorizontal } from "lucide-react";
import { ApiError } from "@/lib/api";
import { ICON_STROKE_WIDTH } from "@/lib/icons";
import { routes } from "@/lib/routes";
import { archiveWorkflow, unarchiveWorkflow, WorkflowArchiveBlocked, type Workflow } from "@/lib/workflows";
import { ConfirmDialog } from "@/components/shared/ConfirmDialog";
import { Button } from "@/components/ui/Button";
import { IconButton } from "@/components/ui/IconButton";
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuTrigger } from "@/components/ui/DropdownMenu";
import { toast } from "@/components/ui/Toaster";
import { TooltipProvider } from "@/components/ui/Tooltip";

export const ARCHIVE_DESCRIPTION =
  "It leaves the workflows list, its schedules are paused, and new runs and schedules are refused until you unarchive it. Its versions, runs and traces stay readable.";

function failure(error: unknown, action: string): string {
  if (error instanceof ApiError && error.status === 404) return "This workflow no longer exists.";
  return error instanceof ApiError && error.message ? `Could not ${action} the workflow: ${error.message}` : `Could not ${action} the workflow. Try again.`;
}

/** The workflow page header's overflow menu: Archive (behind a ConfirmDialog) or Unarchive. */
export function WorkflowArchiveMenu({ workflow, onChanged }: { workflow: Workflow; onChanged: (workflow: Workflow) => void }) {
  const [confirming, setConfirming] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // Set by the API's 409 `runs_active`: shown, with "Archive anyway".
  const [activeRuns, setActiveRuns] = useState<number | null>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);

  const archive = async () => {
    setBusy(true);
    setError(null);
    try {
      onChanged(await archiveWorkflow(workflow.id, { force: activeRuns !== null }));
      setConfirming(false);
      setActiveRuns(null);
      toast.success(`Archived ${workflow.name}`);
    } catch (err) {
      if (err instanceof WorkflowArchiveBlocked) setActiveRuns(err.activeRuns);
      else setError(failure(err, "archive"));
    } finally {
      setBusy(false);
    }
  };
  const close = () => {
    setConfirming(false);
    setError(null);
    setActiveRuns(null);
  };

  return (
    // The workflow page has no TooltipProvider above it (the agent layout does); the trigger needs one.
    <TooltipProvider>
      <DropdownMenu>
        <DropdownMenuTrigger asChild>
          <IconButton
            ref={triggerRef}
            label="More actions"
            size="sm"
            icon={<MoreHorizontal strokeWidth={ICON_STROKE_WIDTH} aria-hidden="true" />}
            data-testid="workflow-more-actions"
          />
        </DropdownMenuTrigger>
        <DropdownMenuContent align="end">
          {workflow.archived ? (
            <DropdownMenuItem data-testid="workflow-unarchive" onSelect={() => void unarchive(workflow, onChanged)}>
              Unarchive workflow
            </DropdownMenuItem>
          ) : (
            <DropdownMenuItem
              destructive
              data-testid="workflow-archive"
              onSelect={() => {
                setError(null);
                setConfirming(true);
              }}
            >
              Archive workflow
            </DropdownMenuItem>
          )}
        </DropdownMenuContent>
      </DropdownMenu>
      {confirming ? (
        <ConfirmDialog
          title={`Archive ${workflow.name}?`}
          description={
            activeRuns !== null ? (
              <div className="space-y-2" data-testid="workflow-archive-runs">
                <p>
                  {activeRuns === 1 ? "1 run is queued or running" : `${activeRuns} runs are queued or running`} on this workflow. Archiving
                  does not stop them.
                </p>
                <p>Wait for them to finish, or archive anyway.</p>
              </div>
            ) : (
              <p>{ARCHIVE_DESCRIPTION}</p>
            )
          }
          confirmLabel={activeRuns !== null ? "Archive anyway" : "Archive workflow"}
          destructive
          busy={busy}
          error={error}
          onConfirm={() => void archive()}
          onCancel={close}
          fallbackFocus={() => triggerRef.current}
        />
      ) : null}
    </TooltipProvider>
  );
}

/** Unarchive (no confirmation: it only makes the workflow usable again); failures are toasts. */
export async function unarchive(workflow: Pick<Workflow, "id" | "name">, onChanged: (workflow: Workflow) => void): Promise<boolean> {
  try {
    onChanged(await unarchiveWorkflow(workflow.id));
    toast.success(`Unarchived ${workflow.name}`);
    return true;
  } catch (err) {
    toast.error(failure(err, "unarchive"));
    return false;
  }
}

/** Shown under the header of an archived workflow's page. */
export function WorkflowArchivedBanner({ workflow, onChanged, mayUnarchive = true }: { workflow: Workflow; onChanged: (workflow: Workflow) => void; mayUnarchive?: boolean }) {
  const [busy, setBusy] = useState(false);
  return (
    <div
      role="status"
      data-testid="workflow-archived-banner"
      className="flex min-w-0 flex-wrap items-center gap-x-3 gap-y-2 border-b border-border bg-muted px-4 py-2 text-label text-muted-foreground md:px-6"
    >
      <span className="min-w-0 flex-1">
        This workflow is archived: new runs and schedules are refused. Its schedules stay paused after you unarchive it. Its history stays
        readable.{" "}
        <Link href={routes.workflows({ archived: true })} className="underline underline-offset-2">
          Archived workflows
        </Link>
      </span>
      {mayUnarchive ? (
        <Button
          size="sm"
          disabled={busy}
          onClick={async () => {
            setBusy(true);
            await unarchive(workflow, onChanged);
            setBusy(false);
          }}
        >
          Unarchive
        </Button>
      ) : null}
    </div>
  );
}
