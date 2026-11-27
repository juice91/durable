"use client";

import { useCan } from "@/lib/permissions";
import React, { useState } from "react";
import { useRouter } from "next/navigation";
import { CatalogEntrySheet } from "@/components/mcp/CatalogEntrySheet";
import { CatalogList } from "@/components/mcp/CatalogList";
import { McpSectionTabs } from "@/components/mcp/McpSectionTabs";
import { McpServerDialog } from "@/components/mcp/McpServerDialog";
import { PageHeader } from "@/components/frame/PageHeader";
import { Button } from "@/components/ui/Button";
import type { CatalogEntry } from "@/lib/mcpCatalog";
import { navCrumb } from "@/lib/nav";
import { routes } from "@/lib/routes";

/** Catalog: the ToolHive-first catalog of MCP servers that can be run here or registered as remote endpoints. */
export default function McpCatalogPage() {
  const router = useRouter();
  const [entry, setEntry] = useState<CatalogEntry | null>(null);
  const [remoteOpen, setRemoteOpen] = useState(false);
  const mayCreate = useCan("mcp_server", null, "create");

  return (
    <>
      <PageHeader
        crumbs={[navCrumb("mcp"), { label: "Catalog" }]}
        documentTitle="MCP catalog"
        actions={mayCreate ? <Button onClick={() => setRemoteOpen(true)}>Remote server by URL</Button> : null}
      />
      <div className="mx-auto flex w-full min-w-0 max-w-7xl flex-col gap-4 px-4 py-6">
        <McpSectionTabs current="browse" />
        <p className="text-body text-muted-foreground">Find an MCP server, run it here or register its hosted endpoint, and give agents its tools.</p>
        <CatalogList onOpen={setEntry} autoFocusSearch />
      </div>
      <CatalogEntrySheet entry={entry} onClose={() => setEntry(null)} onInstalled={(server) => router.push(routes.mcpServer(server.id))} />
      {remoteOpen ? (
        <McpServerDialog onSaved={(server) => { setRemoteOpen(false); if (server) router.push(routes.mcpServer(server.id)); }} onClose={() => setRemoteOpen(false)} />
      ) : null}
    </>
  );
}
