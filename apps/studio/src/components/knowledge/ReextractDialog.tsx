"use client";

import React, { useEffect, useId, useRef, useState } from "react";
import { Button } from "@/components/ui/Button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/Dialog";
import { reextractKnowledgeBase, type ReextractRequest, type ReextractResult } from "@/lib/knowledge";
import { alertClass } from "./knowledgeUi";

export interface ReextractDialogProps {
  kbId: string;
  /** The knowledge base's chunker version: on 1, the dialog offers the move to chunker v2. */
  chunkerVersion: number;
  onStarted: (result: ReextractResult) => void;
  onClose: () => void;
}

/** The request for the dialog's choice: outdated documents only, or (chunker v2) every document. */
export function reextractRequest(moveToV2: boolean, dryRun: boolean): ReextractRequest {
  const body: ReextractRequest = moveToV2 ? { chunker_version: 2 } : { only_outdated: true };
  return dryRun ? { ...body, dry_run: true } : body;
}

/** "3 documents", "1 document". */
function documents(n: number): string {
  return `${n.toLocaleString("en-US")} document${n === 1 ? "" : "s"}`;
}

/**
 * "Re-extract outdated": re-extracts the documents whose recorded extractor version is older than
 * the current one, after a dry run shows how many. On a chunker v1 knowledge base it can also move it
 * to chunker v2, which re-extracts every document. Each document gets a new version and its own
 * ingest run; unchanged sections keep their embeddings, so only changed sections cost anything.
 */
export function ReextractDialog({ kbId, chunkerVersion, onStarted, onClose }: ReextractDialogProps) {
  const id = useId();
  const [moveToV2, setMoveToV2] = useState(false);
  const [preview, setPreview] = useState<ReextractResult | null>(null);
  const [previewError, setPreviewError] = useState<string | null>(null);
  const [starting, setStarting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // The bulk route has no idempotency key (each call makes new versions), so a second submit must
  // never leave while one is in flight: a ref, set synchronously, so two clicks in one frame (before
  // `starting` re-renders the button disabled) still send one request.
  const inFlight = useRef(false);
  // Bumped after a failed start: the dry run is asked again (some documents may have started before
  // the error), and Start stays disabled until it answers.
  const [recount, setRecount] = useState(0);

  useEffect(() => {
    setPreview(null);
    setPreviewError(null);
    let cancelled = false;
    reextractKnowledgeBase(kbId, reextractRequest(moveToV2, true)).then(
      (result) => {
        if (!cancelled) setPreview(result);
      },
      (reason: unknown) => {
        if (!cancelled) setPreviewError(reason instanceof Error ? reason.message : "Could not count the documents");
      },
    );
    return () => {
      cancelled = true;
    };
  }, [kbId, moveToV2, recount]);

  const start = async () => {
    if (!preview || preview.selected === 0 || starting || inFlight.current) return;
    inFlight.current = true;
    setStarting(true);
    setError(null);
    try {
      onStarted(await reextractKnowledgeBase(kbId, reextractRequest(moveToV2, false)));
    } catch (reason: unknown) {
      setError(`${reason instanceof Error ? reason.message : "The re-extract could not be started"} The count was refreshed; check it before trying again.`);
      setRecount((n) => n + 1);
      setStarting(false);
    } finally {
      inFlight.current = false;
    }
  };

  return (
    <Dialog open onOpenChange={(open) => { if (!open && !starting) onClose(); }}>
      <DialogContent className="md:max-w-xl" data-testid="knowledge-reextract-dialog">
        <DialogHeader>
          <DialogTitle>Re-extract outdated documents</DialogTitle>
          <DialogDescription>
            Documents read by an older extractor version get a new version from the same content. Sections whose text did not change keep their embeddings.
          </DialogDescription>
        </DialogHeader>
        <div className="flex min-w-0 flex-col gap-4">
          {chunkerVersion < 2 ? (
            <div className="flex min-w-0 items-start gap-2">
              <input
                id={`${id}-v2`}
                type="checkbox"
                checked={moveToV2}
                onChange={(event) => setMoveToV2(event.target.checked)}
                disabled={starting}
                className="mt-1 size-4 shrink-0 accent-primary"
                data-testid="knowledge-reextract-chunker-v2"
              />
              <label htmlFor={`${id}-v2`} className="min-w-0 text-label text-foreground">
                Also move to chunker v2
                <span className="block text-caption text-muted-foreground">Merges short sections into their parent heading and gives code sections their context. Re-extracts every document.</span>
              </label>
            </div>
          ) : null}
          <div
            data-testid="knowledge-reextract-preview"
            data-state={preview ? "ready" : previewError ? "error" : "loading"}
            aria-live="polite"
            className="rounded-md border border-border bg-surface p-3 text-label"
          >
            {!preview && !previewError ? <p className="text-muted-foreground">Counting documents…</p> : null}
            {previewError ? <p role="alert" className="text-destructive-text">{previewError}</p> : null}
            {preview ? (
              <p data-testid="knowledge-reextract-count">
                {preview.selected === 0
                  ? "Every document is up to date. Nothing to re-extract."
                  : `${documents(preview.selected)} will be re-extracted.`}
              </p>
            ) : null}
          </div>
          {error ? <p role="alert" data-testid="knowledge-reextract-error" className={alertClass}>{error}</p> : null}
        </div>
        <DialogFooter>
          <Button variant="ghost" onClick={onClose} disabled={starting}>Cancel</Button>
          <Button variant="primary" onClick={() => void start()} disabled={!preview || preview.selected === 0 || starting} data-testid="knowledge-reextract-start">
            {starting ? "Starting…" : "Re-extract"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

/** The Documents tab's line after a bulk re-extract started. */
export function reextractStartedText(result: Pick<ReextractResult, "started" | "failed" | "chunker_version_changed">): string {
  const parts = [`Re-extract started for ${documents(result.started)}.`];
  if (result.chunker_version_changed) parts.push("The knowledge base now uses chunker v2.");
  if (result.failed > 0) parts.push(`${documents(result.failed)} could not be queued; Retry them from the list.`);
  return parts.join(" ");
}
