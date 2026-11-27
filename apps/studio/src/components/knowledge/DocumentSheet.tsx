"use client";

import { useKnowledgePermissions } from "./useKnowledgePermissions";
import React, { useEffect, useState } from "react";
import { ConfirmDialog } from "@/components/shared/ConfirmDialog";
import { TraceLink } from "@/components/traces/TraceLink";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { Sheet, SheetBody, SheetContent, SheetHeader } from "@/components/ui/Sheet";
import { ApiError } from "@/lib/api";
import {
  deleteDocument,
  describeDocumentError,
  emptyExtractionText,
  extractionOutdated,
  extractionSummary,
  formatLabel,
  getDocument,
  imageOnlyPagesText,
  isIngestInFlight,
  isSafeHttpUrl,
  newClientRunId,
  pageListText,
  progressLabel,
  displayedDocumentStatus,
  reextractDocument,
  type IngestRunResult,
  type KnowledgeDocument,
  type IngestProgress,
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
  /** Runs whose live stream reported they started: a pending document shows "Ingesting". */
  startedRuns?: ReadonlySet<string>;
  onOpenChange: (open: boolean) => void;
  onDeleted: (documentKey: string) => void;
  /** The document as read here (fresher than the list's). */
  onLoaded: (document: KnowledgeDocument) => void;
  /** Re-extract started a new version and its ingest run (the document is in `result.document`). */
  onReextracted?: (result: IngestRunResult) => void;
  /** How often the Sheet re-reads a document it follows itself (default DOCUMENT_POLL_MS). */
  pollMs?: number;
}

/**
 * While the Sheet's own copy is ingesting and the list has no copy of it (a deep link to a document
 * past the loaded page), the Sheet re-reads it at this interval until the ingest ends, so a finished
 * re-extract shows its new extraction line without reopening.
 */
export const DOCUMENT_POLL_MS = 3_000;

/**
 * One document (`?doc=<key>`): status and error, versions, format, its extraction (extractor and
 * version, pages, image-only pages, warnings) for worker-extracted formats, metadata, source URL, its
 * last ingest run's trace, a content preview (text formats only: a PDF's bytes never come back),
 * Re-extract (confirmed: a new version with the same content) and Delete (confirmed; its sections go
 * with it).
 */
export function DocumentSheet({ kbId, documentKey, listed, progress, startedRuns, onOpenChange, onDeleted, onLoaded, onReextracted, pollMs = DOCUMENT_POLL_MS }: DocumentSheetProps) {
  const mayEdit = useKnowledgePermissions(kbId).can("edit");
  const [loaded, setLoaded] = useState<KnowledgeDocument | null>(null);
  const [notFound, setNotFound] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [content, setContent] = useState<string | null>(null);
  const [contentBusy, setContentBusy] = useState(false);
  const [confirming, setConfirming] = useState(false);
  const [deleting, setDeleting] = useState(false);
  const [deleteError, setDeleteError] = useState<string | null>(null);
  const [reextracting, setReextracting] = useState(false);
  const [reextractBusy, setReextractBusy] = useState(false);
  const [reextractError, setReextractError] = useState<string | null>(null);
  const [reextractNote, setReextractNote] = useState<string | null>(null);
  // One run id per confirm: a double click or a retry after an error re-sends the same id.
  const reextractRunId = React.useRef<string | null>(null);
  // The listed active version the Sheet last re-read the document for (see below).
  const refreshedFor = React.useRef<number | null>(null);
  const onLoadedRef = React.useRef(onLoaded);
  onLoadedRef.current = onLoaded;
  // The Sheet opens with focus on its title, never on Delete (the first tabbable control).
  const titleRef = React.useRef<HTMLSpanElement>(null);
  const focusTitle = (event: Event) => {
    if (!titleRef.current) return;
    event.preventDefault();
    titleRef.current.focus();
  };

  useEffect(() => {
    setLoaded(null);
    setNotFound(false);
    setError(null);
    setContent(null);
    setConfirming(false);
    setReextracting(false);
    setReextractError(null);
    setReextractNote(null);
    refreshedFor.current = null;
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
  // `extraction` comes with the single-document GET only: read it again when the active version moves
  // (a re-extract or a new upload finished), without resetting the Sheet.
  // Once per new listed active version (never in a loop when the two copies disagree).
  const listedActive = listed?.active_version ?? null;
  const hasLoaded = loaded !== null;
  useEffect(() => {
    if (!documentKey || !hasLoaded || listedActive === null) return;
    if (refreshedFor.current === null) {
      refreshedFor.current = listedActive; // the first read already has this version
      return;
    }
    if (refreshedFor.current === listedActive) return;
    refreshedFor.current = listedActive;
    let cancelled = false;
    getDocument(kbId, documentKey).then(
      (fresh) => {
        if (cancelled) return;
        setLoaded(fresh);
        onLoadedRef.current(fresh);
      },
      () => undefined, // the list's copy still shows; the next version change tries again
    );
    return () => {
      cancelled = true;
    };
  }, [kbId, documentKey, listedActive, hasLoaded]);
  // No listed copy (deep link): follow the Sheet's own copy while it ingests. Each answer replaces
  // `loaded`, so the poll stops as soon as the document is ready or failed (and then carries the
  // new version's `extraction`).
  const followSelf = !!documentKey && listed === null && loaded !== null && isIngestInFlight(loaded);
  useEffect(() => {
    if (!followSelf || !documentKey) return;
    let cancelled = false;
    const timer = setTimeout(() => {
      getDocument(kbId, documentKey).then(
        (fresh) => {
          if (cancelled) return;
          setLoaded(fresh);
          onLoadedRef.current(fresh);
        },
        () => {
          // Try again on the next tick: a new object re-arms the timer.
          if (!cancelled) setLoaded((current) => (current ? { ...current } : current));
        },
      );
    }, pollMs);
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, [followSelf, kbId, documentKey, pollMs, loaded]);
  const extraction = loaded?.extraction ?? null;
  const binary = doc?.content_encoding === "base64";
  const imageOnly = extraction?.image_only_pages ?? [];
  const imageOnlyText = imageOnlyPagesText(imageOnly);

  const openReextract = () => {
    reextractRunId.current = newClientRunId();
    setReextractError(null);
    setReextracting(true);
  };

  const reextract = async () => {
    if (!doc || reextractBusy) return;
    setReextractBusy(true);
    setReextractError(null);
    try {
      const result = await reextractDocument(kbId, doc.document_key, reextractRunId.current);
      if (result.document) {
        setLoaded((current) => ({ ...result.document!, extraction: result.document!.extraction ?? current?.extraction ?? null }));
        onLoadedRef.current(result.document);
      }
      onReextracted?.(result);
      setReextractNote(result.document ? `Re-extracting as v${result.document.latest_version}.` : "Re-extract started.");
      setReextracting(false);
    } catch (reason: unknown) {
      setReextractError(reason instanceof Error ? reason.message : "The document could not be re-extracted");
    } finally {
      setReextractBusy(false);
    }
  };

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
        <SheetContent size="md" data-testid="knowledge-document-sheet" onOpenAutoFocus={focusTitle}>
          <SheetHeader
            title={
              <span ref={titleRef} tabIndex={-1} data-testid="knowledge-document-title" className="rounded-sm outline-none focus-visible:ring-2 focus-visible:ring-ring">
                {doc?.title ?? documentKey}
              </span>
            }
            description={<span className="font-mono">{documentKey}</span>}
            actions={
              doc && mayEdit ? (
                <>
                  <Button size="sm" variant="ghost" onClick={openReextract} disabled={isIngestInFlight(doc)} data-testid="knowledge-document-reextract">
                    Re-extract
                  </Button>
                  <Button size="sm" variant="ghost" className="text-destructive-text" onClick={() => setConfirming(true)} data-testid="knowledge-document-delete">
                    Delete
                  </Button>
                </>
              ) : null
            }
          />
          <SheetBody className="flex min-w-0 flex-col gap-4">
            {notFound ? <p role="alert" data-testid="knowledge-document-not-found" className={alertClass}>This document no longer exists.</p> : null}
            {error ? <p role="alert" className={alertClass}>{error}</p> : null}
            {!doc && !notFound && !error ? <p role="status" className="text-body text-muted-foreground">Loading document…</p> : null}
            {reextractNote ? <p role="status" data-testid="knowledge-document-reextract-note" className="text-label text-muted-foreground">{reextractNote}</p> : null}
            {doc ? (
              <>
                {imageOnlyText ? (
                  <p role="note" data-testid="knowledge-document-image-only" className="rounded-md border border-warning/30 bg-warning/10 p-3 text-body text-warning-text">
                    {imageOnlyText} <span className="text-caption">({pageListText(imageOnly)})</span>
                  </p>
                ) : null}
                <dl className="grid min-w-0 grid-cols-1 gap-x-4 gap-y-2 text-label sm:grid-cols-[8rem_minmax(0,1fr)]">
                  <dt className="text-muted-foreground">Status</dt>
                  <dd className="flex min-w-0 flex-col items-start gap-1">
                    <DocumentStatusBadge status={displayedDocumentStatus(doc, startedRuns)} />
                    {isIngestInFlight(doc) && progress ? <span className="text-caption text-muted-foreground">{progressLabel(progress)}</span> : null}
                    {doc.status === "failed" ? <span className="text-caption text-destructive-text" data-testid="knowledge-document-sheet-error">{describeDocumentError(doc)}</span> : null}
                  </dd>
                  <dt className="text-muted-foreground">Sections</dt>
                  <dd className="tabular-nums">{doc.chunk_count}</dd>
                  <dt className="text-muted-foreground">Versions</dt>
                  <dd data-testid="knowledge-document-versions">
                    Active {doc.active_version !== null ? `v${doc.active_version}` : "none yet"} · latest v{doc.latest_version}
                  </dd>
                  <dt className="text-muted-foreground">Format</dt>
                  <dd className="min-w-0 break-words" data-testid="knowledge-document-format">
                    {formatLabel(doc.mime_type)} <span className="font-mono text-caption text-muted-foreground">{doc.mime_type}</span>
                  </dd>
                  {extraction ? (
                    <>
                      <dt className="text-muted-foreground">Extraction</dt>
                      <dd className="flex min-w-0 flex-col items-start gap-1">
                        <span className="break-words" data-testid="knowledge-document-extractor">{extractionSummary(extraction)}</span>
                        {emptyExtractionText(extraction) ? (
                          <span className="break-words text-caption text-muted-foreground" data-testid="knowledge-document-empty">
                            {emptyExtractionText(extraction)}
                          </span>
                        ) : null}
                        {extractionOutdated(extraction) ? (
                          <span className="text-caption text-warning-text" data-testid="knowledge-document-extractor-outdated">
                            Extractor v{extraction.current_version} is available. Re-extract to use it.
                          </span>
                        ) : null}
                      </dd>
                    </>
                  ) : null}
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
                  {binary ? (
                    <p className="text-caption text-muted-foreground" data-testid="knowledge-document-binary">
                      The original {formatLabel(doc.mime_type)} is stored but not shown here.{isSafeHttpUrl(doc.source_url) ? " Open its source URL to read it." : ""}
                    </p>
                  ) : content === null ? (
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
          {reextracting && doc ? (
            <ConfirmDialog
              title={`Re-extract ${doc.title}?`}
              description={
                <p>
                  A new version (v{doc.latest_version + 1}) is made from the same content with the current extractor and the knowledge base&apos;s chunker. Sections whose text did not change keep their embeddings, so only changed sections are embedded again.
                </p>
              }
              confirmLabel="Re-extract"
              busy={reextractBusy}
              error={reextractError}
              onConfirm={() => void reextract()}
              onCancel={() => setReextracting(false)}
            />
          ) : null}
          {confirming && doc ? (
            <ConfirmDialog
              title={`Delete ${doc.title}?`}
              description={<p>{doc.chunk_count === 1 ? "Its 1 section is" : `Its ${doc.chunk_count} sections are`} removed from search at once. An ingest in progress ends as cancelled.</p>}
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
