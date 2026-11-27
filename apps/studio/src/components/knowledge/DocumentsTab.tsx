"use client";

import { useKnowledgePermissions } from "./useKnowledgePermissions";
import React, { useState } from "react";
import { DataList, type DataListColumn } from "@/components/shared/DataList";
import { TraceLink } from "@/components/traces/TraceLink";
import { Button } from "@/components/ui/Button";
import { STUCK_INGEST_TEXT, describeDocumentError, displayedDocumentStatus, formatLabel, isIngestInFlight, progressLabel, reingestDocument, type IngestProgress, type KnowledgeDocument, type ReextractResult } from "@/lib/knowledge";
import { DocumentStatusBadge, formatWhen } from "./knowledgeUi";
import { ReextractDialog, reextractStartedText } from "./ReextractDialog";
import type { KnowledgeDocumentsState } from "./useKnowledgeDocuments";
import { useStuckIngests } from "./useStuckIngests";

// DataList renders this on each row as data-testid (named `testId` so the test-id inventory sees it).
const DOCUMENT_ROW = { testId: "knowledge-document-row" } as const;

export interface DocumentsTabProps {
  kbId: string;
  docs: KnowledgeDocumentsState;
  /** Run id → its latest `ingest_progress` (useIngestProgress). */
  progress: Record<string, IngestProgress>;
  /** Runs whose live stream reported they started (useIngestProgress): "Ingesting" before the re-read. */
  startedRuns?: ReadonlySet<string>;
  /** The open document Sheet's key. */
  openDocument: string | null;
  onOpenDocument: (documentKey: string) => void;
  onUpload: () => void;
  /** The knowledge base's chunker version (`chunkerVersionOf`): on 1, "Re-extract outdated" offers chunker v2. */
  chunkerVersion?: number;
  /** A bulk re-extract started (its runs are queued; the list is re-read here). */
  onReextracted?: (result: ReextractResult) => void;
}

function versionLabel(doc: KnowledgeDocument): string {
  if (doc.active_version === null) return `v${doc.latest_version} (not active yet)`;
  return doc.active_version === doc.latest_version ? `v${doc.active_version}` : `v${doc.active_version} → v${doc.latest_version}`;
}

/**
 * The Documents tab: status with live progress, format, sections, version, Retry for a failed
 * ingest, and "Re-extract outdated" (a dry run, then a new version of each outdated document).
 */
export function DocumentsTab({ kbId, docs, progress, startedRuns, openDocument, onOpenDocument, onUpload, chunkerVersion = 2, onReextracted }: DocumentsTabProps) {
  const mayEdit = useKnowledgePermissions(kbId).can("edit");
  const [reextractOpen, setReextractOpen] = useState(false);
  const [retrying, setRetrying] = useState<Record<string, boolean>>({});
  const [actionError, setActionError] = useState<string | null>(null);
  // A Retry the API answered 409 "already being ingested": the run is going, not an error.
  const [actionInfo, setActionInfo] = useState<string | null>(null);
  // Pending documents whose ingest never started (its hand-off failed): they get Retry too.
  const { stuck, recheck } = useStuckIngests(docs.documents);

  const retry = async (doc: KnowledgeDocument) => {
    setRetrying((current) => ({ ...current, [doc.document_key]: true }));
    setActionError(null);
    setActionInfo(null);
    try {
      const result = await reingestDocument(kbId, doc.document_key);
      // A handed-off-again run keeps its id: read its status afresh.
      if (doc.last_run_id) recheck(doc.last_run_id);
      if (result.run_id) recheck(result.run_id);
      if (result.document) docs.upsert(result.document);
      else docs.refresh(doc.document_key);
    } catch (reason: unknown) {
      const message = reason instanceof Error ? reason.message : "the retry failed";
      if ((reason as { status?: unknown } | null)?.status === 409 && /already being ingested/i.test(message)) {
        setActionInfo(`${doc.title}: the ingest is already running.`);
        if (doc.last_run_id) recheck(doc.last_run_id);
      } else {
        setActionError(`${doc.title}: ${message}`);
      }
      docs.refresh(doc.document_key);
    } finally {
      setRetrying((current) => ({ ...current, [doc.document_key]: false }));
    }
  };

  const columns: DataListColumn<KnowledgeDocument>[] = [
    {
      id: "title",
      header: "Document",
      sortable: true,
      sortValue: (doc) => doc.title,
      cell: (doc) => (
        <span className="flex min-w-0 flex-col">
          <span className="truncate font-medium">{doc.title}</span>
          {doc.title !== doc.document_key ? <span className="truncate font-mono text-caption text-muted-foreground">{doc.document_key}</span> : null}
        </span>
      ),
    },
    {
      id: "status",
      header: "Status",
      sortable: true,
      sortValue: (doc) => doc.status,
      className: "min-w-0 max-w-xs",
      cell: (doc) => {
        const live = doc.last_run_id ? progress[doc.last_run_id] : undefined;
        return (
          <span className="flex min-w-0 flex-col items-start gap-0.5">
            <DocumentStatusBadge status={displayedDocumentStatus(doc, startedRuns)} />
            {stuck.has(doc.document_key) ? (
              <span className="line-clamp-2 text-caption text-warning-text" data-testid="knowledge-document-stuck">
                {STUCK_INGEST_TEXT[stuck.get(doc.document_key)!]}
              </span>
            ) : isIngestInFlight(doc) && live ? (
              <span className="text-caption tabular-nums text-muted-foreground" data-testid="knowledge-document-progress" aria-live="polite">
                {progressLabel(live)}
              </span>
            ) : null}
            {doc.status === "failed" ? (
              <span className="line-clamp-2 text-caption text-destructive-text" data-testid="knowledge-document-error" title={doc.error ?? undefined}>
                {describeDocumentError(doc)}
              </span>
            ) : null}
          </span>
        );
      },
    },
    {
      id: "format",
      header: "Format",
      sortable: true,
      sortValue: (doc) => formatLabel(doc.mime_type),
      className: "hidden whitespace-nowrap sm:table-cell",
      cell: (doc) => <span className="text-caption text-muted-foreground" data-testid="knowledge-document-format-cell">{formatLabel(doc.mime_type)}</span>,
    },
    {
      id: "sections",
      header: "Sections",
      align: "end",
      sortable: true,
      sortValue: (doc) => doc.chunk_count,
      cell: (doc) => <span className="tabular-nums" data-testid="knowledge-document-sections">{doc.chunk_count}</span>,
    },
    {
      id: "version",
      header: "Version",
      className: "hidden whitespace-nowrap md:table-cell",
      cell: (doc) => <span className="text-caption text-muted-foreground">{versionLabel(doc)}</span>,
    },
    {
      id: "updated",
      header: "Updated",
      sortable: true,
      sortValue: (doc) => doc.updated_at,
      className: "hidden whitespace-nowrap lg:table-cell",
      cell: (doc) => <span className="text-caption text-muted-foreground">{formatWhen(doc.updated_at)}</span>,
    },
    {
      id: "actions",
      header: <span className="sr-only">Actions</span>,
      align: "end",
      className: "whitespace-nowrap",
      cell: (doc) => (
        <span className="flex items-center justify-end gap-2">
          {mayEdit && (doc.status === "failed" || stuck.has(doc.document_key)) ? (
            <Button size="sm" disabled={retrying[doc.document_key]} onClick={() => void retry(doc)} data-testid="knowledge-document-retry" aria-label={`Retry ingest of ${doc.title}`}>
              {retrying[doc.document_key] ? "Retrying…" : "Retry"}
            </Button>
          ) : null}
          {doc.last_run_id ? (
            <TraceLink runId={doc.last_run_id} className="text-caption text-info-text hover:underline" data-testid="knowledge-document-trace" aria-label={`Ingest trace of ${doc.title}`}>
              Trace
            </TraceLink>
          ) : null}
        </span>
      ),
    },
  ];

  return (
    <div className="flex min-w-0 flex-col gap-3" data-testid="knowledge-documents-tab">
      {mayEdit && docs.documents.length > 0 ? (
        <div className="flex min-w-0 flex-wrap justify-end gap-2">
          <Button size="sm" variant="secondary" onClick={() => setReextractOpen(true)} data-testid="knowledge-documents-reextract">
            Re-extract outdated
          </Button>
        </div>
      ) : null}
      {actionInfo ? <p role="status" data-testid="knowledge-documents-info" className="rounded-md border border-border bg-fill-hover p-3 text-body text-muted-foreground">{actionInfo}</p> : null}
      {actionError ? <p role="alert" data-testid="knowledge-documents-error" className="rounded-md border border-destructive/30 bg-destructive/10 p-3 text-body text-destructive-text">{actionError}</p> : null}
      <DataList
        label="Documents"
        testId="knowledge-documents"
        rows={docs.documents}
        columns={columns}
        getRowId={(doc) => doc.document_key}
        onRowOpen={(doc) => onOpenDocument(doc.document_key)}
        getRowLabel={(doc) => `${doc.title}, ${doc.status}`}
        getRowTestId={() => DOCUMENT_ROW.testId}
        selectedRowId={openDocument}
        searchPlaceholder={docs.hasMore ? "Search loaded documents by title or key" : "Search by title or key"}
        filterRow={(doc, query) => {
          const q = query.toLowerCase();
          return doc.title.toLowerCase().includes(q) || doc.document_key.toLowerCase().includes(q);
        }}
        loading={docs.loading}
        error={docs.error}
        onRetry={docs.reload}
        empty={{
          title: "No documents yet",
          description: "Upload Markdown, text, HTML, reStructuredText, PDF, YAML or JSON files, or paste text. Each is split into sections and embedded.",
          action: mayEdit ? <Button variant="primary" onClick={onUpload}>Upload documents</Button> : undefined,
        }}
      />
      {docs.hasMore && !docs.loading ? (
        <div className="flex justify-center">
          <Button variant="secondary" size="sm" onClick={docs.loadMore} disabled={docs.loadingMore} data-testid="knowledge-documents-more">
            {docs.loadingMore ? "Loading…" : "Load more"}
          </Button>
        </div>
      ) : null}
      {reextractOpen ? (
        <ReextractDialog
          kbId={kbId}
          chunkerVersion={chunkerVersion}
          onClose={() => setReextractOpen(false)}
          onStarted={(result) => {
            setReextractOpen(false);
            setActionError(null);
            setActionInfo(reextractStartedText(result));
            docs.reload();
            onReextracted?.(result);
          }}
        />
      ) : null}
    </div>
  );
}
