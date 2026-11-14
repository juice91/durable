"use client";

import React, { useEffect, useState } from "react";
import { ConfirmDialog } from "@/components/shared/ConfirmDialog";
import { TraceLink } from "@/components/traces/TraceLink";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { Sheet, SheetBody, SheetContent, SheetHeader } from "@/components/ui/Sheet";
import { ApiError } from "@/lib/api";
import {
  deleteDocument,
  describeIngestError,
  getDocument,
  isIngestInFlight,
  isSafeHttpUrl,
  progressLabel,
  type IngestProgress,
  type KnowledgeDocument,
} from "@/lib/knowledge";
import { alertClass, DocumentStatusBadge, formatWhen } from "./knowledgeUi";

/** Characters of the content shown in the preview. */
export const CONTENT_PREVIEW_CHARS = 20_000;

export interface DocumentSheetProps {
  kbId: string;
  /** The open document's key (from the URL), or null. */
  documentKey: string | null;
  /** The list's copy of it, if loaded (kept fresh by the poll); the Sheet reads it again on open. */
  listed: KnowledgeDocument | null;
  progress: IngestProgress | null;
  onOpenChange: (open: boolean) => void;
  onDeleted: (documentKey: string) => void;
  /** The document as read here (fresher than the list's). */
  onLoaded: (document: KnowledgeDocument) => void;
}

/**
 * One document (`?doc=<key>`): status and error, versions, metadata, source URL, its last ingest
 * run's trace, a content preview, and Delete (confirmed; its sections go with it).
 */
export function DocumentSheet({ kbId, documentKey, listed, progress, onOpenChange, onDeleted, onLoaded }: DocumentSheetProps) {
  const [loaded, setLoaded] = useState<KnowledgeDocument | null>(null);
  const [notFound, setNotFound] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [content, setContent] = useState<string | null>(null);
  const [contentBusy, setContentBusy] = useState(false);
  const [confirming, setConfirming] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [deleteError, setDeleteError] = useState<string | null>(null);
  const onLoadedRef = React.useRef(onLoaded);
  onLoadedRef.current = onLoaded;

  useEffect(() => {
    setLoaded(null);
    setNotFound(false);
    setError(null);
    setContent(null);
    setConfirming(false);
    if (!documentKey) return;
    let cancelled = false;
    getDocument(kbId, documentKey).then(
      (doc) => {
        if (cancelled) return;
        setLoaded(doc);
        onLoadedRef.current(doc);
      },
      (reason: unknown) => {
        if (cancelled) return;
        if (reason instanceof ApiError && reason.status === 404) setNotFound(true);
        else setError(reason instanceof Error ? reason.message : "Failed to load the document");
      },
    );
    return () => {
      cancelled = true;
    };
  }, [kbId, documentKey]);

  // The list's copy is refreshed while it ingests; prefer whichever is newer.
  const doc = listed && (!loaded || listed.updated_at >= loaded.updated_at) ? listed : loaded;

  const showContent = async () => {
    if (!documentKey) return;
    setContentBusy(true);
    try {
      const full = await getDocument(kbId, documentKey, { includeContent: true });
      setContent(full.content ?? "");
    } catch (reason: unknown) {
      setError(reason instanceof Error ? reason.message : "Failed to load the content");
    } finally {
      setContentBusy(false);
    }
  };

  const remove = async () => {
    if (!doc || deleting) return;
    setDeleting(true);
    setDeleteError(null);
    try {
      await deleteDocument(kbId, doc.document_key);
      onDeleted(doc.document_key);
      setConfirming(false);
      onOpenChange(false);
    } catch (reason: unknown) {
      if (reason instanceof ApiError && reason.status === 404) {
        onDeleted(doc.document_key);
        setConfirming(false);
        onOpenChange(false);
      } else {
        setDeleteError(reason instanceof Error ? reason.message : "The document could not be deleted");
      }
    } finally {
      setDeleting(false);
    }
  };

  const metadata = doc ? Object.entries(doc.metadata ?? {}) : [];

  return (
    <Sheet open={documentKey !== null} onOpenChange={onOpenChange}>
      {documentKey !== null ? (
        <SheetContent size="md" data-testid="knowledge-document-sheet">
          <SheetHeader
            title={doc?.title ?? documentKey}
            description={<span className="font-mono">{documentKey}</span>}
            actions={
              doc ? (
                <Button size="sm" variant="ghost" className="text-destructive-text" onClick={() => setConfirming(true)} data-testid="knowledge-document-delete">
                  Delete
                </Button>
              ) : null
            }
          />
          <SheetBody className="flex min-w-0 flex-col gap-4">
            {notFound ? <p role="alert" data-testid="knowledge-document-not-found" className={alertClass}>This document no longer exists.</p> : null}
            {error ? <p role="alert" className={alertClass}>{error}</p> : null}
            {!doc && !notFound && !error ? <p role="status" className="text-body text-muted-foreground">Loading document…</p> : null}
            {doc ? (
              <>
                <dl className="grid min-w-0 grid-cols-1 gap-x-4 gap-y-2 text-label sm:grid-cols-[8rem_minmax(0,1fr)]">
                  <dt className="text-muted-foreground">Status</dt>
                  <dd className="flex min-w-0 flex-col items-start gap-1">
                    <DocumentStatusBadge status={doc.status} />
                    {isIngestInFlight(doc) && progress ? <span className="text-caption text-muted-foreground">{progressLabel(progress)}</span> : null}
                    {doc.status === "failed" ? <span className="text-caption text-destructive-text">{describeIngestError(doc.error)}</span> : null}
                  </dd>
                  <dt className="text-muted-foreground">Sections</dt>
                  <dd className="tabular-nums">{doc.chunk_count}</dd>
                  <dt className="text-muted-foreground">Versions</dt>
                  <dd data-testid="knowledge-document-versions">
                    Active {doc.active_version !== null ? `v${doc.active_version}` : "none yet"} · latest v{doc.latest_version}
                  </dd>
                  <dt className="text-muted-foreground">Format</dt>
                  <dd className="font-mono">{doc.mime_type}</dd>
                  <dt className="text-muted-foreground">Source</dt>
                  <dd className="min-w-0 break-all">
                    {isSafeHttpUrl(doc.source_url) ? (
                      <a href={doc.source_url} target="_blank" rel="noopener noreferrer nofollow" className="text-info-text underline-offset-4 hover:underline">{doc.source_url}</a>
                    ) : (
                      <span className="text-muted-foreground">None</span>
                    )}
                  </dd>
                  <dt className="text-muted-foreground">Metadata</dt>
                  <dd className="flex min-w-0 flex-wrap gap-1" data-testid="knowledge-document-metadata">
                    {metadata.length === 0 ? <span className="text-muted-foreground">None</span> : metadata.map(([key, value]) => <Badge key={key} variant="neutral" className="font-mono">{key}: {String(value)}</Badge>)}
                  </dd>
                  <dt className="text-muted-foreground">Last ingest</dt>
                  <dd>
                    {doc.last_run_id ? (
                      <TraceLink runId={doc.last_run_id} className="font-mono text-info-text hover:underline" data-testid="knowledge-document-run">
                        {doc.last_run_id.slice(0, 8)}
                      </TraceLink>
                    ) : <span className="text-muted-foreground">None</span>}
                  </dd>
                  <dt className="text-muted-foreground">Updated</dt>
                  <dd>{formatWhen(doc.updated_at)}</dd>
                  <dt className="text-muted-foreground">Content hash</dt>
                  <dd className="min-w-0 truncate font-mono text-caption" title={doc.content_hash}>{doc.content_hash}</dd>
                </dl>
                <section aria-label="Content" className="flex min-w-0 flex-col gap-2">
                  {content === null ? (
                    <div>
                      <Button size="sm" onClick={() => void showContent()} disabled={contentBusy} data-testid="knowledge-document-show-content">
                        {contentBusy ? "Loading…" : "Show content"}
                      </Button>
                    </div>
                  ) : (
                    <>
                      <pre data-testid="knowledge-document-content" className="max-h-96 min-w-0 overflow-auto whitespace-pre-wrap break-words rounded-md border border-border bg-fill-hover p-3 font-mono text-caption text-foreground">
                        {content.slice(0, CONTENT_PREVIEW_CHARS)}
                      </pre>
                      {content.length > CONTENT_PREVIEW_CHARS ? <p className="text-caption text-muted-foreground">Showing the first {CONTENT_PREVIEW_CHARS.toLocaleString("en-US")} characters.</p> : null}
                    </>
                  )}
                </section>
              </>
            ) : null}
          </SheetBody>
          {confirming && doc ? (
            <ConfirmDialog
              title={`Delete ${doc.title}?`}
              description={<p>Its {doc.chunk_count} section{doc.chunk_count === 1 ? "" : "s"} are removed from search at once. An ingest in progress ends as cancelled.</p>}
              confirmLabel="Delete document"
              destructive
              busy={deleting}
              error={deleteError}
              onConfirm={() => void remove()}
              onCancel={() => setConfirming(false)}
            />
          ) : null}
        </SheetContent>
      ) : null}
    </Sheet>
  );
}
