"use client";

import React, { useState } from "react";
import { useRouter } from "next/navigation";
import { CatalogEntrySheet } from "@/components/mcp/CatalogEntrySheet";
import { CatalogList } from "@/components/mcp/CatalogList";
import { McpSectionTabs } from "@/components/mcp/McpSectionTabs";
import { PageHeader } from "@/components/frame/PageHeader";
import type { CatalogEntry } from "@/lib/mcpCatalog";
import { navCrumb } from "@/lib/nav";
import { routes } from "@/lib/routes";

/** Browse: the catalog of MCP servers that can be run here or registered as remote endpoints. */
export default function McpBrowsePage() {
  const router = useRouter();
  const [entry, setEntry] = useState<CatalogEntry | null>(null);

  return (
    <>
      <PageHeader crumbs={[navCrumb("mcp"), { label: "Browse" }]} documentTitle="Browse MCP servers" />
      <div className="mx-auto flex w-full min-w-0 max-w-7xl flex-col gap-4 px-4 py-6">
        <McpSectionTabs current="browse" />
        <p className="text-body text-muted-foreground">Find an MCP server, run it here or register its hosted endpoint, and give agents its tools.</p>
        <CatalogList onOpen={setEntry} />
      </div>
      <CatalogEntrySheet entry={entry} onClose={() => setEntry(null)} onInstalled={(server) => router.push(routes.mcpServer(server.id))} />
    </>
  );
}
