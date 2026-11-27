"use client";

/**
 * The workflow page's draft status: a notice when the draft differs from the latest published
 * version (or nothing is published yet), "Review changes" (draft vs live, by step and raw JSON) and
 * "Publish vN+1" behind a confirmation dialog. The compare is canonical, so key order never counts.
 */
import React, { useMemo, useRef, useState } from "react";
import { ApiError, ConflictError } from "@/lib/api";
import {
  canonicalJson,
  draftDiffers,
  lineDiff,
  summarizeChanges,
  type WorkflowChanges,
} from "@/lib/workflowDraft";
import { getWorkflow, publishWorkflow, type PublishedWorkflowVersion, type Workflow, type WorkflowVersion } from "@/lib/workflows";
import { ConfirmDialog } from "@/components/shared/ConfirmDialog";
import { Button } from "@/components/ui/Button";
import { Sheet, SheetBody, SheetContent, SheetHeader } from "@/components/ui/Sheet";
import { toast } from "@/components/ui/Toaster";
import { PublishWarnings } from "@/components/agents/PublishWarnings";
import { sideBySideRows } from "@/components/agents/VersionCompare";
import { publishWarningsOf } from "@/components/agents/agentCapabilities";

export const NOT_PUBLISHED_TEXT = "Not published yet: this workflow has a draft only. Publish it to run it.";

export function unpublishedText(version: number): string {
  return `This workflow has unpublished changes (draft differs from v${version})`;
}

function ChangeSummary({ changes, live }: { changes: WorkflowChanges; live: boolean }) {
  if (!live) return <p>The draft has no published version to compare with.</p>;
  if (changes.total === 0) return <p>No step or spec changes.</p>;
  return (
    <ul className="list-inside list-disc space-y-1 break-words" data-testid="workflow-change-summary">
      {changes.added.length > 0 ? <li data-testid="workflow-changes-added">Added steps: {changes.added.join(", ")}</li> : null}
      {changes.removed.length > 0 ? <li data-testid="workflow-changes-removed">Removed steps: {changes.removed.join(", ")}</li> : null}
      {changes.changed.map((c) => (
        <li key={c.id} data-testid="workflow-changes-changed">Changed: {c.id} ({c.fields.join("; ")})</li>
      ))}
      {changes.specFields.length > 0 ? <li data-testid="workflow-changes-spec">Workflow settings changed: {changes.specFields.join(", ")}</li> : null}
    </ul>
  );
}

function countsLine(changes: WorkflowChanges): string {
  return `${changes.added.length} added, ${changes.removed.length} removed, ${changes.changed.length} changed step${changes.changed.length === 1 ? "" : "s"}${changes.specFields.length > 0 ? `, ${changes.specFields.length} workflow setting${changes.specFields.length === 1 ? "" : "s"} changed` : ""}`;
}

function ReviewSheet({ workflow, live, changes, onClose, onPublish, canPublish }: {
  workflow: Workflow;
  live: WorkflowVersion | null;
  changes: WorkflowChanges;
  onClose: () => void;
  onPublish: () => void;
  canPublish: boolean;
}) {
  const [raw, setRaw] = useState(false);
  const rows = useMemo(
    () => (raw ? sideBySideRows(lineDiff(live ? canonicalJson(live.spec, 2) : "", canonicalJson(workflow.draft_spec, 2))) : []),
    [raw, live, workflow.draft_spec],
  );
  const liveLabel = live ? `v${live.version}` : "nothing";
  return (
    <Sheet open onOpenChange={(open) => (open ? undefined : onClose())}>
      <SheetContent size="wide" aria-describedby={undefined} data-testid="workflow-review-sheet">
        <SheetHeader
          title={`Review changes: ${liveLabel} → draft`}
          actions={canPublish ? <Button size="sm" onClick={onPublish} data-testid="workflow-review-publish">Publish v{(workflow.latest_version ?? 0) + 1}</Button> : null}
        />
        <SheetBody>
          <div className="space-y-4">
            <ChangeSummary changes={changes} live={live !== null} />
            <Button size="sm" variant="secondary" aria-pressed={raw} data-testid="workflow-raw-toggle" onClick={() => setRaw((v) => !v)}>
              {raw ? "Hide raw JSON diff" : "Show raw JSON diff"}
            </Button>
            {raw ? (
              <div className="overflow-x-auto rounded-md border border-border">
                <table className="w-full table-fixed font-mono text-xs" data-testid="workflow-raw-diff">
                  <caption className="sr-only">Raw JSON diff, {liveLabel} on the left, draft on the right</caption>
                  <thead className="bg-muted">
                    <tr>
                      <th scope="col" className="px-2 py-1 text-left font-sans text-xs font-medium text-muted-foreground">{liveLabel}</th>
                      <th scope="col" className="px-2 py-1 text-left font-sans text-xs font-medium text-muted-foreground">Draft</th>
                    </tr>
                  </thead>
                  <tbody className="bg-card text-foreground">
                    {rows.map((row, i) => (
                      <tr key={i}>
                        {(["base", "target"] as const).map((side) => {
                          const line = row[side];
                          const tone = !line || line.op === "equal" ? "" : line.op === "removed" ? "bg-destructive/10" : "bg-primary/10";
                          return (
                            <td key={side} className={`w-1/2 px-2 py-0.5 align-top ${tone}`} data-op={line?.op ?? "none"}>
                              {line && line.op !== "equal" ? <span className="sr-only">{line.op}: </span> : null}
                              <span className="whitespace-pre-wrap break-words">{line?.text ?? ""}</span>
                            </td>
                          );
                        })}
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ) : null}
          </div>
        </SheetBody>
      </SheetContent>
    </Sheet>
  );
}

export interface WorkflowDraftBarProps {
  workflow: Workflow;
  /** The latest published version, or null when none is published. */
  live: WorkflowVersion | null;
  /** Called after a publish, a 409 or a 412 so the page reloads the workflow and its versions. */
  onChanged: () => void;
  /** Once IAM policy applies this says whether the caller may publish; today everyone may. */
  canPublish?: boolean;
}

export function WorkflowDraftBar({ workflow, live, onChanged, canPublish = true }: WorkflowDraftBarProps) {
  const [reviewing, setReviewing] = useState(false);
  const [confirming, setConfirming] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [published, setPublished] = useState<PublishedWorkflowVersion | null>(null);
  const publishRef = useRef<HTMLButtonElement>(null);

  const draft = workflow.draft_spec;
  const dirty = draftDiffers(draft, live?.spec);
  const changes = useMemo(() => (draft ? summarizeChanges(live?.spec, draft) : null), [draft, live]);
  const nextVersion = (workflow.latest_version ?? 0) + 1;

  const closeConfirm = () => { setConfirming(false); setError(null); };

  const publish = async () => {
    setBusy(true);
    setError(null);
    try {
      // Publish takes no If-Match on the API today: check the revision here, so a draft edited after
      // this page loaded is never published unseen.
      const fresh = await getWorkflow(workflow.id);
      if (workflow.revision !== undefined && fresh.revision !== undefined && fresh.revision !== workflow.revision) {
        setConfirming(false);
        setReviewing(false);
        setNotice("Someone else changed this workflow after you opened it. The page now shows the current draft: review it and publish again.");
        onChanged();
        return;
      }
      const result = await publishWorkflow(workflow.id);
      setConfirming(false);
      setReviewing(false);
      setNotice(null);
      setPublished(result);
      toast.success(`Published ${workflow.name} v${result.version}`);
      onChanged();
    } catch (err) {
      if (err instanceof ConflictError) {
        setConfirming(false);
        setNotice("Someone else changed this workflow while you were publishing. The page now shows the current draft: review it and publish again.");
        onChanged();
      } else if (err instanceof ApiError && err.status === 409) {
        setConfirming(false);
        setNotice(live ? `Nothing to publish: the draft is the same as v${live.version}.` : "Nothing to publish.");
        onChanged();
      } else {
        setError(err instanceof ApiError && err.message ? `Could not publish: ${err.message}` : "Could not publish the workflow. Try again.");
      }
    } finally {
      setBusy(false);
    }
  };

  const publishedWarnings = published ? publishWarningsOf(published) : [];
  return (
    <>
      {published ? (
        <div role="status" data-testid="workflow-published-notice" className="space-y-2 border-b border-border bg-muted px-4 py-2 text-label text-foreground md:px-6">
          <p>Published v{published.version}.</p>
          <PublishWarnings warnings={publishedWarnings} />
        </div>
      ) : null}
      {notice ? (
        <p role="alert" data-testid="workflow-publish-notice" className="border-b border-border bg-warning/10 px-4 py-2 text-label text-warning-text md:px-6">{notice}</p>
      ) : null}
      {draft && !workflow.archived && (dirty || !live) ? (
        <div
          role="status"
          data-testid="workflow-draft-notice"
          className="flex min-w-0 flex-wrap items-center gap-x-3 gap-y-2 border-b border-border bg-warning/10 px-4 py-2 text-label text-warning-text md:px-6"
        >
          <span className="min-w-0 flex-1">{live ? unpublishedText(live.version) : NOT_PUBLISHED_TEXT}</span>
          <Button size="sm" variant="secondary" data-testid="workflow-review-changes" onClick={() => setReviewing(true)}>Review changes</Button>
          {canPublish ? (
            <Button ref={publishRef} size="sm" data-testid="workflow-publish" onClick={() => setConfirming(true)}>Publish v{nextVersion}</Button>
          ) : null}
        </div>
      ) : null}
      {reviewing && changes ? (
        <ReviewSheet
          workflow={workflow}
          live={live}
          changes={changes}
          canPublish={canPublish}
          onClose={() => setReviewing(false)}
          onPublish={() => setConfirming(true)}
        />
      ) : null}
      {confirming && changes ? (
        <ConfirmDialog
          title={`Publish v${nextVersion} of ${workflow.name}?`}
          description={
            <div className="space-y-2" data-testid="workflow-publish-confirm">
              <p>{live ? `Live v${live.version} will be replaced for new runs by the draft: ${countsLine(changes)}.` : "The draft becomes the first published version."}</p>
              {live ? <ChangeSummary changes={changes} live /> : null}
              <p>A published version never changes. Any publish warnings are shown after it is published.</p>
            </div>
          }
          confirmLabel={`Publish v${nextVersion}`}
          busy={busy}
          error={error}
          onConfirm={() => void publish()}
          onCancel={closeConfirm}
          fallbackFocus={() => publishRef.current}
        />
      ) : null}
    </>
  );
}
