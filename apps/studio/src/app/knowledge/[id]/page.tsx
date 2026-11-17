"use client";

import React, { Suspense, useCallback, useEffect, useMemo, useRef, useState } from "react";
import Link from "next/link";
import { useParams, useRouter, useSearchParams } from "next/navigation";
import { RefreshCw, Trash2, Upload } from "lucide-react";
import { PageHeader } from "@/components/frame/PageHeader";
import { DocumentSheet } from "@/components/knowledge/DocumentSheet";
import { DocumentsTab } from "@/components/knowledge/DocumentsTab";
import { IngestRunsTab } from "@/components/knowledge/IngestRunsTab";
import { KnowledgeDeleteFlow } from "@/components/knowledge/KnowledgeDeleteFlow";
import { ingestCostLabel, KnowledgeBaseStatusBadge, knowledgeTabs } from "@/components/knowledge/knowledgeUi";
import { ReindexDialog } from "@/components/knowledge/ReindexDialog";
import { SearchTab } from "@/components/knowledge/SearchTab";
import { SourcesTab } from "@/components/knowledge/SourcesTab";
import { UploadSheet } from "@/components/knowledge/UploadSheet";
import { useIngestProgress } from "@/components/knowledge/useIngestProgress";
import { useKnowledgeDocuments } from "@/components/knowledge/useKnowledgeDocuments";
import { EmptyState } from "@/components/shared/EmptyState";
import { TraceLink } from "@/components/traces/TraceLink";
import { TraceSheetHost } from "@/components/traces/TraceSheetHost";
import { Button } from "@/components/ui/Button";
import { useUrlSheet } from "@/components/ui/useUrlSheet";
import { ApiError } from "@/lib/api";
import { formatTokens } from "@/lib/format";
import { ICON_STROKE_WIDTH } from "@/lib/icons";
import {
  cancelReindex,
  chunkerVersionOf,
  describeIngestError,
  getKnowledgeBase,
  isIngestInFlight,
  listKnowledgeBaseRuns,
  listKnowledgeBases,
  progressLabel,
  startReindex,
  type KnowledgeBase,
  type KnowledgeBaseDetail,
} from "@/lib/knowledge";
import { navCrumb } from "@/lib/nav";
import { routes } from "@/lib/routes";
import {
  KNOWLEDGE_DOC_PARAM,
  KNOWLEDGE_TAB_PARAM,
  KNOWLEDGE_TRACE_TAB_PARAM,
  parseKnowledgeTab,
} from "@/lib/routes/knowledge";

function isReindexInput(input: string): boolean {
  try {
    return typeof (JSON.parse(input) as { reindex?: unknown }).reindex === "number";
  } catch {
    return false;
  }
}

function KnowledgeBaseDetailView() {
  const params = useParams();
  const router = useRouter();
  const searchParams = useSearchParams();
  const tab = parseKnowledgeTab(searchParams?.get(KNOWLEDGE_TAB_PARAM));
  const id = typeof params.id === "string" ? params.id : "";
  const docSheet = useUrlSheet(KNOWLEDGE_DOC_PARAM, { history: "push" });

  const [kb, setKb] = useState<KnowledgeBaseDetail | null>(null);
  const [notFound, setNotFound] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [allKbs, setAllKbs] = useState<KnowledgeBase[]>([]);
  const [uploadOpen, setUploadOpen] = useState(false);
  const [reindexOpen, setReindexOpen] = useState(false);
  const [deleteOpen, setDeleteOpen] = useState(false);
  const [reindexRunId, setReindexRunId] = useState<string | null>(null);
  const [reindexBusy, setReindexBusy] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  const [runsRefresh, setRunsRefresh] = useState(0);
  const kbSeq = useRef(0);

  const loadKb = useCallback(async () => {
    if (!id) return;
    const ticket = ++kbSeq.current;
    try {
      const detail = await getKnowledgeBase(id);
      if (ticket !== kbSeq.current) return;
      setKb(detail);
      setNotFound(false);
      setLoadError(null);
    } catch (reason: unknown) {
      if (ticket !== kbSeq.current) return;
      if (reason instanceof ApiError && reason.status === 404) setNotFound(true);
      else setLoadError(reason instanceof Error ? reason.message : "Failed to load the knowledge base");
    }
  }, [id]);

  useEffect(() => {
    setKb(null);
    setReindexRunId(null);
    void loadKb();
    return () => {
      kbSeq.current += 1;
    };
  }, [loadKb]);

  // The breadcrumb switcher's options; a failure leaves the switcher with this one.
  useEffect(() => {
    let cancelled = false;
    listKnowledgeBases().then(
      (list) => {
        if (!cancelled) setAllKbs(list);
      },
      () => undefined,
    );
    return () => {
      cancelled = true;
    };
  }, []);

  // A reindex in progress (or failed) when the page opens: find its run to follow it.
  const pendingGeneration = kb?.pending_generation ?? null;
  useEffect(() => {
    if (!id || pendingGeneration === null || reindexRunId) return;
    let cancelled = false;
    listKnowledgeBaseRuns(id, { limit: 20 }).then(
      (page) => {
        const run = page.items.find((item) => isReindexInput(item.input));
        if (!cancelled && run) setReindexRunId(run.run_id);
      },
      () => undefined,
    );
    return () => {
      cancelled = true;
    };
  }, [id, pendingGeneration, reindexRunId]);

  const docs = useKnowledgeDocuments(id, () => {
    void loadKb();
    setRunsRefresh((n) => n + 1);
  });

  const followed = useMemo(() => {
    const ids = docs.documents.filter(isIngestInFlight).flatMap((doc) => (doc.last_run_id ? [doc.last_run_id] : []));
    return kb?.status === "reindexing" && reindexRunId ? [reindexRunId, ...ids] : ids;
  }, [docs.documents, kb?.status, reindexRunId]);

  const documentByRun = useRef(new Map<string, string>());
  documentByRun.current = new Map(docs.documents.flatMap((doc) => (doc.last_run_id ? [[doc.last_run_id, doc.document_key] as [string, string]] : [])));

  const { progress, started: startedRuns } = useIngestProgress(followed, (runId) => {
    const key = documentByRun.current.get(runId);
    if (key) docs.refresh(key);
    if (runId === reindexRunId) void loadKb();
    setRunsRefresh((n) => n + 1);
  });

  const cancelPendingReindex = async () => {
    if (!kb) return;
    setReindexBusy(true);
    setActionError(null);
    try {
      await cancelReindex(kb.id);
      setReindexRunId(null);
      await loadKb();
    } catch (reason: unknown) {
      setActionError(reason instanceof Error ? reason.message : "The reindex could not be cancelled");
    } finally {
      setReindexBusy(false);
    }
  };

  const retryReindex = async () => {
    if (!kb?.pending_embedding_model) return;
    setReindexBusy(true);
    setActionError(null);
    try {
      const result = await startReindex(kb.id, kb.pending_embedding_model);
      setReindexRunId(result.run_id);
      setRunsRefresh((n) => n + 1);
      await loadKb();
    } catch (reason: unknown) {
      setActionError(reason instanceof Error ? reason.message : "The reindex could not be retried");
    } finally {
      setReindexBusy(false);
    }
  };

  const listCrumb = navCrumb("knowledge");
  if (notFound) {
    return (
      <>
        <PageHeader crumbs={[listCrumb, { label: "Not found" }]} />
        <div className="mx-auto w-full min-w-0 max-w-3xl px-4 py-6">
          <EmptyState
            testId="knowledge-not-found"
            title="Knowledge base not found"
            description="It may have been deleted, or the link is wrong."
            action={<Button asChild><Link href={routes.knowledge()}>Back to Knowledge</Link></Button>}
          />
        </div>
      </>
    );
  }

  const switcherOptions = (allKbs.length > 0 ? allKbs : kb ? [kb] : [])
    .map((item) => ({ value: item.id, label: item.name }))
    .sort((a, b) => a.label.localeCompare(b.label));
  const reindexProgress = reindexRunId ? progress[reindexRunId] : undefined;
  const openDocument = docSheet.value;
  const listedDocument = openDocument ? docs.documents.find((doc) => doc.document_key === openDocument) ?? null : null;

  return (
    <>
      <PageHeader
        crumbs={[
          listCrumb,
          {
            label: kb?.name ?? "Knowledge base",
            switcher: kb
              ? { label: "Switch knowledge base", options: switcherOptions, value: kb.id, hrefFor: (next) => routes.knowledgeBase(next, { tab }), testId: "knowledge-switcher" }
              : undefined,
          },
        ]}
        documentTitle={kb?.name}
        tabs={knowledgeTabs(id, tab)}
        tabsLabel="Knowledge base sections"
        actions={
          kb ? (
            <>
              <Button variant="primary" onClick={() => setUploadOpen(true)} data-testid="knowledge-upload">
                <Upload strokeWidth={ICON_STROKE_WIDTH} aria-hidden="true" />
                Upload
              </Button>
              <Button onClick={() => setReindexOpen(true)} disabled={kb.status === "reindexing"} data-testid="knowledge-reindex">
                <RefreshCw strokeWidth={ICON_STROKE_WIDTH} aria-hidden="true" />
                <span className="hidden sm:inline">Reindex</span>
                <span className="sr-only sm:hidden">Reindex</span>
              </Button>
              <Button variant="ghost" className="text-destructive-text" onClick={() => setDeleteOpen(true)} data-testid="knowledge-delete">
                <Trash2 strokeWidth={ICON_STROKE_WIDTH} aria-hidden="true" />
                <span className="hidden sm:inline">Delete</span>
                <span className="sr-only sm:hidden">Delete</span>
              </Button>
            </>
          ) : null
        }
      />
      <div className="mx-auto flex w-full min-w-0 max-w-7xl flex-col gap-4 px-4 py-6">
        {loadError ? (
          <div role="alert" className="flex flex-wrap items-center justify-between gap-3 rounded-lg border border-destructive/30 bg-destructive/10 p-3 text-body text-destructive-text">
            <span className="min-w-0">{loadError}</span>
            <Button size="sm" onClick={() => void loadKb()}>Retry</Button>
          </div>
        ) : null}
        {!kb && !loadError ? <p className="text-body text-muted-foreground" role="status">Loading knowledge base…</p> : null}

        {kb ? (
          <>
            {kb.description ? <p className="text-body text-muted-foreground">{kb.description}</p> : null}
            <dl data-testid="knowledge-summary" className="grid min-w-0 grid-cols-[repeat(auto-fit,minmax(min(100%,10rem),1fr))] gap-3">
              <div className="min-w-0 rounded-lg border border-border bg-surface p-3">
                <dt className="text-caption text-muted-foreground">Embedding model</dt>
                <dd className="truncate font-mono text-label" title={`${kb.embedding_model} (${kb.dimensions} dims)`} data-testid="knowledge-summary-model">{kb.embedding_model}</dd>
                <dd className="text-caption text-muted-foreground">{kb.dimensions} dims</dd>
              </div>
              <div className="min-w-0 rounded-lg border border-border bg-surface p-3">
                <dt className="text-caption text-muted-foreground">Documents</dt>
                <dd className="text-title tabular-nums" data-testid="knowledge-summary-documents">{kb.document_count ?? 0}</dd>
              </div>
              <div className="min-w-0 rounded-lg border border-border bg-surface p-3">
                <dt className="text-caption text-muted-foreground">Sections</dt>
                <dd className="text-title tabular-nums" data-testid="knowledge-summary-sections">{kb.chunk_count ?? 0}</dd>
              </div>
              <div className="min-w-0 rounded-lg border border-border bg-surface p-3">
                <dt className="text-caption text-muted-foreground">Ingest cost</dt>
                <dd className="text-title tabular-nums" data-testid="knowledge-ingest-cost">{ingestCostLabel(kb.ingest_cost_usd, true)}</dd>
                <dd className="text-caption text-muted-foreground" data-testid="knowledge-ingest-tokens">
                  {formatTokens(kb.ingest_tokens)} tokens · {kb.ingest_requests} request{kb.ingest_requests === 1 ? "" : "s"}
                </dd>
              </div>
              <div className="min-w-0 rounded-lg border border-border bg-surface p-3">
                <dt className="text-caption text-muted-foreground">Status</dt>
                <dd className="pt-1"><KnowledgeBaseStatusBadge status={kb.status} /></dd>
              </div>
            </dl>

            {kb.status !== "ready" ? (
              <div
                role={kb.status === "reindex_failed" ? "alert" : "status"}
                data-testid="knowledge-reindex-banner"
                data-status={kb.status}
                className={kb.status === "reindex_failed"
                  ? "flex min-w-0 flex-wrap items-center gap-3 rounded-lg border border-destructive/30 bg-destructive/10 p-3 text-body text-destructive-text"
                  : "flex min-w-0 flex-wrap items-center gap-3 rounded-lg border border-border bg-muted/40 p-3 text-body text-foreground"}
              >
                <span className="min-w-0 flex-1">
                  {kb.status === "reindexing"
                    ? <>Reindexing with <span className="font-mono">{kb.pending_embedding_model}</span>{reindexProgress ? `: ${progressLabel(reindexProgress)}` : "…"} Search uses the current embeddings until it finishes.</>
                    : <>The reindex with <span className="font-mono">{kb.pending_embedding_model}</span> failed: {describeIngestError(kb.error)}</>}
                </span>
                {reindexRunId ? <TraceLink runId={reindexRunId} className="text-label text-info-text hover:underline" data-testid="knowledge-reindex-trace">Trace</TraceLink> : null}
                {kb.status === "reindex_failed" ? (
                  <Button size="sm" onClick={() => void retryReindex()} disabled={reindexBusy} data-testid="knowledge-reindex-retry">Retry reindex</Button>
                ) : null}
                <Button size="sm" variant="ghost" onClick={() => void cancelPendingReindex()} disabled={reindexBusy} data-testid="knowledge-reindex-cancel">Cancel reindex</Button>
              </div>
            ) : null}
            {actionError ? <p role="alert" data-testid="knowledge-action-error" className="rounded-md border border-destructive/30 bg-destructive/10 p-3 text-body text-destructive-text">{actionError}</p> : null}

            {tab === "documents" ? (
              <DocumentsTab
                kbId={kb.id}
                docs={docs}
                progress={progress}
                startedRuns={startedRuns}
                openDocument={openDocument}
                onOpenDocument={(key) => docSheet.open(key)}
                onUpload={() => setUploadOpen(true)}
                chunkerVersion={chunkerVersionOf(kb)}
                onReextracted={() => {
                  setRunsRefresh((n) => n + 1);
                  void loadKb();
                }}
              />
            ) : null}
            {tab === "search" ? <SearchTab kbId={kb.id} /> : null}
            {tab === "sources" ? <SourcesTab kbId={kb.id} kbName={kb.name} /> : null}
            {tab === "runs" ? <IngestRunsTab kbId={kb.id} documents={docs.documents} refreshKey={runsRefresh} /> : null}
          </>
        ) : null}
      </div>

      {kb ? (
        <>
          <UploadSheet
            open={uploadOpen}
            onOpenChange={setUploadOpen}
            kbId={kb.id}
            kbName={kb.name}
            onUploaded={(result) => {
              docs.upsert(result.document);
              setRunsRefresh((n) => n + 1);
              if (!result.unchanged) void loadKb();
            }}
          />
          <DocumentSheet
            kbId={kb.id}
            documentKey={openDocument}
            listed={listedDocument}
            progress={listedDocument?.last_run_id ? progress[listedDocument.last_run_id] ?? null : null}
            startedRuns={startedRuns}
            onOpenChange={docSheet.onOpenChange}
            onReextracted={(result) => {
              if (result.document) docs.upsert(result.document);
              setRunsRefresh((n) => n + 1);
            }}
            onLoaded={(doc) => {
              if (docs.documents.some((item) => item.document_key === doc.document_key)) docs.upsert(doc);
            }}
            onDeleted={(key) => {
              docs.remove(key);
              void loadKb();
            }}
          />
          {reindexOpen ? (
            <ReindexDialog
              kb={kb}
              onClose={() => setReindexOpen(false)}
              onStarted={(result) => {
                setReindexOpen(false);
                setReindexRunId(result.run_id);
                setRunsRefresh((n) => n + 1);
                void loadKb();
              }}
            />
          ) : null}
          {deleteOpen ? (
            <KnowledgeDeleteFlow
              kb={kb}
              onCancel={() => setDeleteOpen(false)}
              onDeleted={() => router.push(routes.knowledge())}
            />
          ) : null}
        </>
      ) : null}
    </>
  );
}

/**
 * `/knowledge/:id` (RM.16): the knowledge base header (switcher; Upload, Reindex, Delete), its
 * totals and ingest cost, and the tabs Documents (default), Search (`?tab=search`) and Ingest runs
 * (`?tab=runs`) and Sources (`?tab=sources`). `?doc=<key>` opens a document's Sheet; the page hosts the trace Sheet, whose tab
 * parameter is `traceTab` (`tab` is the page's own).
 */
export default function KnowledgeBasePage() {
  // useSearchParams (the tab, the document and trace Sheets) needs a Suspense boundary for the static prerender.
  return (
    <Suspense fallback={<p className="p-4 text-muted-foreground">Loading knowledge base…</p>}>
      <TraceSheetHost tabParam={KNOWLEDGE_TRACE_TAB_PARAM}>
        <KnowledgeBaseDetailView />
      </TraceSheetHost>
    </Suspense>
  );
}
