"use client";

import React, { Suspense, useCallback, useEffect, useMemo, useState } from "react";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { PageHeader } from "@/components/frame/PageHeader";
import { DataList, type DataListColumn } from "@/components/shared/DataList";
import { RunsStartedElsewhere } from "@/components/tools/RunsStartedElsewhere";
import { ToolSheet } from "@/components/tools/ToolSheet";
import { buildToolRows, findToolRow, sourceLabel, toolUsageByKey, type ToolRow } from "@/components/tools/toolCatalog";
import { Badge } from "@/components/ui/Badge";
import { useUrlSheet, withSearchParams } from "@/components/ui/useUrlSheet";
import { describeUsage, usageCountLabel, useAgentLatestSpecs, type AgentUsage } from "@/lib/agentUsage";
import { navCrumb } from "@/lib/nav";
import type { ToolTab } from "@/lib/routes/tools";
import { listTools, type ToolDefinition } from "@/lib/tools";

// DataList renders this on each row as data-testid (named `testId` so the test-id inventory sees it).
const TOOL_ROW = { testId: "tool-row" } as const;
const NO_USAGE: AgentUsage[] = [];

/**
 * Tools: a DataList of the built-in catalog plus the MCP tools agents use. A row sets
 * `?tool=<name>` (and `&server=` for an MCP tool) and opens the tool Sheet with Overview and Run
 * (`&tab=run`) tabs, so a reload or a shared link reopens it. Nothing here is created, so the page
 * registers no `c` action.
 */
export default function ToolsPage() {
  // useSearchParams needs a Suspense boundary, or a static build bails out of prerendering the route.
  return (
    <Suspense fallback={null}>
      <ToolsPageContent />
    </Suspense>
  );
}

function ToolsPageContent() {
  const [catalog, setCatalog] = useState<ToolDefinition[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const scan = useAgentLatestSpecs();
  const sheet = useUrlSheet("tool", { clear: ["server", "tab"], history: "push" });
  const router = useRouter();
  const pathname = usePathname() ?? "";
  const searchParams = useSearchParams();
  const server = searchParams?.get("server") || null;
  const tab: ToolTab = searchParams?.get("tab") === "run" ? "run" : "overview";

  const load = useCallback(() => {
    setLoading(true);
    setError(null);
    listTools()
      .then(setCatalog)
      .catch((reason: unknown) => setError(reason instanceof Error ? reason.message : "Failed to load tools"))
      .finally(() => setLoading(false));
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const rows = useMemo(() => buildToolRows(catalog, scan.agents, scan.latestSpecs), [catalog, scan.agents, scan.latestSpecs]);
  const usage = useMemo(() => toolUsageByKey(scan.agents, scan.latestSpecs), [scan.agents, scan.latestSpecs]);
  const usageOf = (row: ToolRow) => (scan.status === "ready" ? usage.get(row.key) ?? NO_USAGE : NO_USAGE);
  const selected = findToolRow(rows, sheet.value, server);
  // MCP rows come from the agents scan, built-in rows from the catalog.
  const selectedLoading = !selected && (loading || (server !== null && scan.status === "loading"));

  const openRow = (row: ToolRow) => {
    // An MCP tool has no Run tab; a built-in tool keeps the tab that is showing.
    if (row.source.kind === "mcp") sheet.open(row.name, { server: row.source.server, tab: null });
    else sheet.open(row.name, { server: null });
  };

  const setTab = (next: ToolTab) => {
    const current = typeof window !== "undefined" && window.location.pathname === pathname ? window.location.search : searchParams?.toString() ?? "";
    router.replace(withSearchParams(pathname, new URLSearchParams(current), { tab: next === "run" ? "run" : null }), { scroll: false });
  };

  const columns: DataListColumn<ToolRow>[] = [
    {
      id: "name",
      header: "Name",
      sortable: true,
      sortValue: (row) => row.title,
      cell: (row) => (
        <span className="flex min-w-0 flex-col gap-0.5">
          <span className="truncate">{row.title}</span>
          {row.title !== row.name ? <span className="truncate font-mono text-caption font-normal text-muted-foreground">{row.name}</span> : null}
          {row.definition ? (
            <span className="mt-1 flex flex-wrap gap-1 font-normal">
              {row.definition.side_effect ? <Badge variant="warning">Side effect</Badge> : <Badge>Read only</Badge>}
              {row.definition.allowed ? <Badge variant="success">Allowed</Badge> : <Badge variant="destructive">Disallowed</Badge>}
            </span>
          ) : null}
        </span>
      ),
    },
    {
      id: "source",
      header: "Source",
      sortable: true,
      sortValue: (row) => (row.source.kind === "builtin" ? "" : row.source.server),
      className: "whitespace-nowrap",
      cell: (row) => (
        <span data-testid="tool-source" className={row.source.kind === "builtin" ? "text-muted-foreground" : "font-mono text-caption text-foreground"}>
          {sourceLabel(row.source)}
        </span>
      ),
    },
    {
      id: "description",
      header: "Description",
      className: "hidden md:table-cell md:max-w-md",
      headerClassName: "hidden md:table-cell",
      cell: (row) => <span className="line-clamp-2 text-muted-foreground">{row.description || "—"}</span>,
    },
    {
      id: "used-by",
      header: "Used by",
      align: "end",
      className: "whitespace-nowrap",
      sortable: scan.status === "ready",
      sortValue: (row) => usageOf(row).length,
      cell: (row) => {
        if (scan.status !== "ready") return <span className="text-muted-foreground" title={scan.status === "error" ? "Could not load agents" : undefined}>—</span>;
        const list = usageOf(row);
        const partialNote = scan.partial ? "Some published versions could not be read; other agents may also use it." : null;
        const names = list.map((item) => `${item.agentName} (${describeUsage(item)})`).join("\n");
        return (
          <span
            data-testid="tool-used-by-count"
            data-count={list.length}
            data-partial={scan.partial || undefined}
            className={list.length === 0 ? "text-muted-foreground" : undefined}
            title={[names, partialNote].filter(Boolean).join("\n") || undefined}
          >
            {usageCountLabel(list.length, scan.partial)}
            {partialNote ? <span className="sr-only"> ({partialNote})</span> : null}
          </span>
        );
      },
    },
  ];

  return (
    <>
      <PageHeader crumbs={[navCrumb("tools", { current: true })]} />
      <div className="mx-auto flex w-full min-w-0 max-w-7xl flex-col gap-4 px-4 py-6">
        <p className="text-body text-muted-foreground">The platform&apos;s built-in tools and the MCP tools agents use. Open one to see its schema and who uses it, or to run it.</p>
        {sheet.isOpen ? null : <RunsStartedElsewhere />}
        <DataList
          label="Tool catalog"
          testId="tool-list"
          rows={rows}
          columns={columns}
          getRowId={(row) => row.key}
          onRowOpen={openRow}
          selectedRowId={selected?.key ?? null}
          getRowLabel={(row) => `${row.title}, ${sourceLabel(row.source)}`}
          getRowTestId={() => TOOL_ROW.testId}
          searchPlaceholder="Search by name, id or description"
          searchLabel="Search tools"
          filterRow={(row, query) => {
            const q = query.toLowerCase();
            return `${row.title} ${row.name} ${row.description} ${sourceLabel(row.source)}`.toLowerCase().includes(q);
          }}
          noMatchText="No tools match your search."
          loading={loading}
          error={error}
          onRetry={load}
          empty={{ title: "No tools", description: "The platform's tool catalog is empty." }}
        />
      </div>
      <ToolSheet
        name={sheet.value}
        server={server}
        row={selected}
        loading={selectedLoading}
        tab={tab}
        onTabChange={setTab}
        onOpenChange={sheet.onOpenChange}
        usedBy={selected ? usageOf(selected) : NO_USAGE}
        usageStatus={scan.status}
        usagePartial={scan.partial}
        onRetryUsage={scan.reload}
        loadError={error}
        onRetryLoad={load}
      />
    </>
  );
}
