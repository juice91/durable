"use client";

import React, { useCallback, useEffect, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import { Plus } from "lucide-react";
import { PageHeader } from "@/components/frame/PageHeader";
import { KnowledgeBaseDialog } from "@/components/knowledge/KnowledgeBaseDialog";
import { ingestCostLabel, KnowledgeBaseStatusBadge } from "@/components/knowledge/knowledgeUi";
import { DataList, type DataListColumn } from "@/components/shared/DataList";
import { Button } from "@/components/ui/Button";
import { Kbd } from "@/components/ui/Kbd";
import { useCreateAction } from "@/components/shortcuts/ShortcutsProvider";
import { mapSettledWithLimit } from "@/lib/agentUsage";
import { formatTokens, parseCost } from "@/lib/format";
import { ICON_STROKE_WIDTH } from "@/lib/icons";
import { getKnowledgeBase, listKnowledgeBases, type KnowledgeBase } from "@/lib/knowledge";
import { navCrumb } from "@/lib/nav";
import { routes } from "@/lib/routes";

/** Ingest cost requests (GET /knowledge-bases/{id}) in flight at once. */
const COST_CONCURRENCY = 4;

// DataList renders this on each row as data-testid (named `testId` so the test-id inventory sees it).
const KB_ROW = { testId: "knowledge-base-row" } as const;

interface CostState {
  cost: string | null;
  tokens: number;
}

/**
 * Knowledge: the tenant's knowledge bases (RM.16). A DataList; a row opens `/knowledge/{id}`;
 * "New knowledge base" (`c`) opens a Dialog with the allowlisted embedding-model picker. The ingest
 * cost column comes from each knowledge base's detail (the list endpoint has no totals).
 */
export default function KnowledgePage() {
  const router = useRouter();
  const [items, setItems] = useState<KnowledgeBase[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const [costs, setCosts] = useState<Record<string, CostState>>({});
  const loadSeq = useRef(0);
  // The cost fetches of the latest load; aborted by the next load and on unmount.
  const costAbort = useRef<AbortController | null>(null);

  const load = useCallback(async () => {
    const ticket = ++loadSeq.current;
    setLoading(true);
    try {
      const list = await listKnowledgeBases();
      if (ticket !== loadSeq.current) return;
      setItems(list);
      setError(null);
      // Costs fill in as they arrive; a failed detail leaves "—".
      costAbort.current?.abort();
      const controller = new AbortController();
      costAbort.current = controller;
      void mapSettledWithLimit(list, COST_CONCURRENCY, async (kb) => {
        if (controller.signal.aborted) return;
        const detail = await getKnowledgeBase(kb.id, { signal: controller.signal });
        if (ticket === loadSeq.current) setCosts((current) => ({ ...current, [kb.id]: { cost: detail.ingest_cost_usd, tokens: detail.ingest_tokens } }));
      });
    } catch (reason: unknown) {
      if (ticket === loadSeq.current) setError(reason instanceof Error ? reason.message : "Failed to load knowledge bases");
    } finally {
      if (ticket === loadSeq.current) setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
    return () => {
      loadSeq.current += 1;
      costAbort.current?.abort();
    };
  }, [load]);

  const openCreate = useCallback(() => setCreating(true), []);
  useCreateAction(creating ? null : openCreate);

  const columns: DataListColumn<KnowledgeBase>[] = [
    {
      id: "name",
      header: "Name",
      sortable: true,
      sortValue: (kb) => kb.name,
      cell: (kb) => (
        <span className="flex min-w-0 flex-col">
          <span className="truncate font-medium">{kb.name}</span>
          {kb.description ? <span className="truncate text-caption text-muted-foreground">{kb.description}</span> : null}
        </span>
      ),
    },
    {
      id: "model",
      header: "Embedding model",
      sortable: true,
      sortValue: (kb) => kb.embedding_model,
      className: "hidden max-w-xs md:table-cell",
      cell: (kb) => <span className="block truncate font-mono text-caption text-muted-foreground" title={`${kb.embedding_model} (${kb.dimensions} dims)`}>{kb.embedding_model}</span>,
    },
    {
      id: "documents",
      header: "Documents",
      align: "end",
      sortable: true,
      sortValue: (kb) => kb.document_count ?? 0,
      cell: (kb) => <span className="tabular-nums" data-testid="knowledge-base-documents">{kb.document_count ?? 0}</span>,
    },
    {
      id: "sections",
      header: "Sections",
      align: "end",
      sortable: true,
      sortValue: (kb) => kb.chunk_count ?? 0,
      className: "hidden sm:table-cell",
      cell: (kb) => <span className="tabular-nums" data-testid="knowledge-base-sections">{kb.chunk_count ?? 0}</span>,
    },
    {
      id: "status",
      header: "Status",
      sortable: true,
      sortValue: (kb) => kb.status,
      cell: (kb) => <KnowledgeBaseStatusBadge status={kb.status} />,
    },
    {
      id: "cost",
      header: "Ingest cost",
      align: "end",
      sortable: true,
      sortValue: (kb) => parseCost(costs[kb.id]?.cost) ?? undefined,
      className: "hidden whitespace-nowrap lg:table-cell",
      cell: (kb) => {
        const cost = costs[kb.id];
        return (
          <span className="tabular-nums text-muted-foreground" data-testid="knowledge-base-cost" title={cost ? `${formatTokens(cost.tokens)} tokens` : undefined}>
            {ingestCostLabel(cost?.cost, cost !== undefined)}
          </span>
        );
      },
    },
  ];

  return (
    <>
      <PageHeader
        crumbs={[navCrumb("knowledge", { current: true })]}
        actions={
          <Button variant="primary" onClick={openCreate} aria-keyshortcuts="c" data-testid="knowledge-new">
            <Plus strokeWidth={ICON_STROKE_WIDTH} aria-hidden="true" />
            New knowledge base
            <Kbd className="hidden md:inline-flex">C</Kbd>
          </Button>
        }
      />
      <div className="mx-auto flex w-full min-w-0 max-w-7xl flex-col gap-4 px-4 py-6">
        <p className="text-body text-muted-foreground">Documents agents can search and cite. Upload Markdown, text, HTML, reStructuredText, PDF, YAML or JSON; each is split into sections and embedded.</p>
        <DataList
          label="Knowledge bases"
          testId="knowledge-list"
          rows={items}
          columns={columns}
          getRowId={(kb) => kb.id}
          getRowHref={(kb) => routes.knowledgeBase(kb.id)}
          getRowLabel={(kb) => kb.name}
          getRowTestId={() => KB_ROW.testId}
          searchPlaceholder="Search by name or description"
          filterRow={(kb, query) => {
            const q = query.toLowerCase();
            return kb.name.toLowerCase().includes(q) || (kb.description ?? "").toLowerCase().includes(q) || kb.embedding_model.toLowerCase().includes(q);
          }}
          defaultSort={{ columnId: "name", direction: "asc" }}
          loading={loading}
          error={error}
          onRetry={() => void load()}
          empty={{
            title: "No knowledge bases yet",
            description: "Create one, upload documents, then attach it to an agent in the editor's Knowledge step.",
            action: <Button variant="primary" onClick={openCreate}>New knowledge base</Button>,
          }}
        />
      </div>
      {creating ? (
        <KnowledgeBaseDialog
          onClose={() => setCreating(false)}
          onCreated={(kb) => {
            setCreating(false);
            router.push(routes.knowledgeBase(kb.id));
          }}
        />
      ) : null}
    </>
  );
}
