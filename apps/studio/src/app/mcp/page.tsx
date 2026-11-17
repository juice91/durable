"use client";

import React, { useCallback, useEffect, useRef, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { ChevronDown, Plus } from "lucide-react";
import { McpDeleteConfirm } from "@/components/mcp/McpDeleteConfirm";
import { McpLastTest } from "@/components/mcp/McpLastTest";
import { McpSectionTabs } from "@/components/mcp/McpSectionTabs";
import { McpServerDialog } from "@/components/mcp/McpServerDialog";
import { RuntimeStateChip } from "@/components/mcp/RuntimePanel";
import { describeUsage, usageCountLabel, useMcpUsage } from "@/components/mcp/mcpUsage";
import { DataList, type DataListColumn } from "@/components/shared/DataList";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { DropdownMenu, DropdownMenuContent, DropdownMenuItem, DropdownMenuTrigger } from "@/components/ui/DropdownMenu";
import { Kbd } from "@/components/ui/Kbd";
import { PageHeader } from "@/components/frame/PageHeader";
import { useCreateAction } from "@/components/shortcuts/ShortcutsProvider";
import { ApiError } from "@/lib/api";
import { ICON_STROKE_WIDTH } from "@/lib/icons";
import { describeMcpApiMessage, mcpServers, type McpServer, type McpTestResult as TestResult } from "@/lib/mcpServers";
import { navCrumb } from "@/lib/nav";
import { routes } from "@/lib/routes";

type FormState = { mode: "create" } | { mode: "edit"; server: McpServer } | null;

function errorMessage(reason: unknown, fallback: string): string {
  if (reason instanceof ApiError) return describeMcpApiMessage(reason.message);
  return reason instanceof Error ? reason.message : fallback;
}

// DataList renders this on each row as data-testid (named `testId` so the test-id inventory sees it).
const SERVER_ROW = { testId: "mcp-server-row" } as const;

const TRANSPORT_LABEL: Record<McpServer["transport"], string> = { streamable_http: "Streamable HTTP" };

/**
 * MCP Servers: the remote servers Studio's agents CONNECT to (client registrations), not servers we
 * serve. A DataList with row actions; a row opens `/mcp/{id}`; "Add server" (`c`) opens a Dialog.
 */
export default function McpServersPage() {
  const [servers, setServers] = useState<McpServer[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [form, setForm] = useState<FormState>(null);
  const [busy, setBusy] = useState<Record<string, boolean>>({});
  // Connection tests run on this page: the Tools column shows their counts (the API keeps no tool list).
  const [tests, setTests] = useState<Record<string, TestResult>>({});
  const [confirmDelete, setConfirmDelete] = useState<McpServer | null>(null);
  const [deleteError, setDeleteError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const router = useRouter();
  const [addMenuOpen, setAddMenuOpen] = useState(false);
  const usage = useMcpUsage();
  // The server whose delete dialog is open right now (state is stale inside async callbacks).
  const openDeleteId = useRef<string | null>(null);
  // After a delete the button that opened the dialog is gone; focus lands on the page heading.
  const headingRef = useRef<HTMLHeadingElement>(null);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      setServers((await mcpServers.list()).slice().sort((a, b) => a.name.localeCompare(b.name)));
      setError(null);
    } catch (reason: unknown) {
      setError(errorMessage(reason, "Failed to load MCP servers"));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const openCreate = useCallback(() => setForm({ mode: "create" }), []);
  // `c` goes through the global shortcut provider (one listener, one guard), only while no dialog is open.
  const openAddMenu = useCallback(() => setAddMenuOpen(true), []);
  useCreateAction(form === null && confirmDelete === null ? openAddMenu : null);
  const openCatalog = useCallback(() => router.push(routes.mcpBrowse()), [router]);

  const replaceServer = (server: McpServer) =>
    setServers((current) => {
      const exists = current.some((item) => item.id === server.id);
      const next = exists ? current.map((item) => (item.id === server.id ? server : item)) : [...current, server];
      return next.sort((a, b) => a.name.localeCompare(b.name));
    });

  const dropTest = (id: string) =>
    setTests((current) => {
      if (!(id in current)) return current;
      const next = { ...current };
      delete next[id];
      return next;
    });

  const withBusy = async (id: string, action: () => Promise<void>) => {
    setBusy((current) => ({ ...current, [id]: true }));
    setActionError(null);
    try {
      await action();
    } catch (reason: unknown) {
      setActionError(errorMessage(reason, "The action failed"));
    } finally {
      setBusy((current) => ({ ...current, [id]: false }));
    }
  };

  const testServer = (server: McpServer) => withBusy(server.id, async () => {
    const result = await mcpServers.test(server.id);
    setTests((current) => ({ ...current, [server.id]: result }));
    // The test records last_status; reload the row so the list shows it.
    replaceServer(await mcpServers.get(server.id));
  });

  const toggleEnabled = (server: McpServer) => withBusy(server.id, async () => {
    replaceServer(await mcpServers.setEnabled(server.id, !server.enabled));
  });

  const openDeleteDialog = (server: McpServer | null) => {
    openDeleteId.current = server?.id ?? null;
    setDeleteError(null);
    setConfirmDelete(server);
    // Always re-check: an agent may have started using it since the page loaded. Delete waits for it.
    if (server) usage.recheck();
  };

  const forgetServer = (id: string) => {
    // A deleted server's edit form has nothing left to save.
    setForm((current) => (current?.mode === "edit" && current.server.id === id ? null : current));
    setServers((current) => current.filter((item) => item.id !== id));
    dropTest(id);
  };

  const closeDeleteDialogAfter = (server: McpServer) => {
    // Only when this dialog is still the open one; otherwise leave focus where the user put it.
    if (openDeleteId.current !== server.id) return;
    openDeleteDialog(null);
    setTimeout(() => headingRef.current?.focus(), 0);
  };

  // Errors stay inside the dialog: the page behind it is inert and would not announce them.
  const deleteServer = async (server: McpServer) => {
    if (busy[server.id]) return;
    setBusy((current) => ({ ...current, [server.id]: true }));
    setDeleteError(null);
    setNotice(null);
    try {
      await mcpServers.remove(server.id);
      forgetServer(server.id);
      closeDeleteDialogAfter(server);
    } catch (reason: unknown) {
      if (reason instanceof ApiError && reason.status === 404) {
        forgetServer(server.id);
        setNotice(`${server.name} was already deleted.`);
        closeDeleteDialogAfter(server);
        void load();
      } else if (openDeleteId.current === server.id) {
        setDeleteError(errorMessage(reason, "The server could not be deleted"));
      } else {
        setActionError(errorMessage(reason, "The server could not be deleted"));
      }
    } finally {
      setBusy((current) => ({ ...current, [server.id]: false }));
    }
  };

  const onSaved = (server: McpServer | null) => {
    const editing = form?.mode === "edit" ? form.server : null;
    setForm(null);
    if (!server) return; // unchanged edit: keep the row as the list has it now
    // Changing the URL or headers clears the stored test result; drop the stale tool count too.
    if (editing && (editing.url !== server.url || server.last_status === null)) dropTest(server.id);
    if (editing) {
      // An edit never re-adds a row that was deleted meanwhile.
      setServers((current) => current.map((item) => (item.id === server.id ? server : item)));
    } else {
      replaceServer(server);
    }
  };

  // Rebuilt each render: the row actions read the latest busy, test and usage state.
  const columns: DataListColumn<McpServer>[] = [
    {
      id: "name",
      header: "Name",
      sortable: true,
      sortValue: (server) => server.name,
      cell: (server) => (
        <span className="inline-flex min-w-0 items-center gap-2">
          <span className="truncate">{server.name}</span>
          <Badge variant={server.enabled ? "success" : "neutral"}>{server.enabled ? "Enabled" : "Disabled"}</Badge>
          {server.runtime === "managed" && server.runtime_state ? <RuntimeStateChip state={server.runtime_state} testId="mcp-runtime-chip" /> : null}
        </span>
      ),
    },
    {
      id: "transport",
      header: "Transport",
      className: "hidden whitespace-nowrap md:table-cell",
      cell: (server) => <span className="text-muted-foreground">{TRANSPORT_LABEL[server.transport] ?? server.transport}</span>,
    },
    {
      id: "url",
      header: "URL",
      sortable: true,
      sortValue: (server) => server.url,
      className: "max-w-xs",
      cell: (server) => <span className="block truncate font-mono text-caption text-muted-foreground" title={server.url}>{server.url}</span>,
    },
    {
      id: "last-test",
      header: "Last test",
      sortable: true,
      sortValue: (server) => server.last_status ?? "",
      cell: (server) => <div data-testid="mcp-last-test"><McpLastTest server={server} showTime={false} /></div>,
    },
    {
      id: "tools",
      header: "Tools",
      align: "end",
      cell: (server) => {
        const result = tests[server.id];
        if (!result || result.status !== "ok") return <span className="text-muted-foreground" title="Test the connection to list its tools">—</span>;
        return <span data-testid="mcp-tool-count">{result.tools.length}</span>;
      },
    },
    {
      id: "used-by",
      header: "Used by",
      align: "end",
      sortable: usage.status === "ready",
      sortValue: (server) => usage.usageFor(server.name).length,
      cell: (server) => {
        if (usage.status !== "ready") return <span className="text-muted-foreground" title={usage.status === "error" ? "Could not load agents" : undefined}>—</span>;
        const list = usage.usageFor(server.name);
        const partialNote = usage.partial ? "Some published versions could not be read; other agents may also use it." : null;
        if (list.length === 0) {
          return (
            <span className="text-muted-foreground" title={partialNote ?? undefined}>
              {usageCountLabel(0, usage.partial)}
              {partialNote ? <span className="sr-only"> ({partialNote})</span> : null}
            </span>
          );
        }
        const names = list.map((item) => `${item.agentName} (${describeUsage(item)})`).join("\n");
        return (
          <span data-testid="mcp-used-by-count" data-partial={usage.partial || undefined} title={partialNote ? `${names}\n${partialNote}` : names}>
            {usageCountLabel(list.length, usage.partial)}
            {partialNote ? <span className="sr-only"> ({partialNote})</span> : null}
          </span>
        );
      },
    },
    {
      id: "actions",
      header: <span className="sr-only">Actions</span>,
      align: "end",
      className: "whitespace-nowrap",
      cell: (server) => (
        <div className="flex items-center justify-end gap-1" data-testid="mcp-row-actions">
          {busy[server.id] ? <span className="text-caption text-muted-foreground" aria-live="polite">Working…</span> : null}
          <Button size="sm" disabled={busy[server.id]} onClick={() => void testServer(server)}>Test connection</Button>
          <Button size="sm" variant="ghost" disabled={busy[server.id]} onClick={() => void toggleEnabled(server)}>{server.enabled ? "Disable" : "Enable"}</Button>
          <Button size="sm" variant="ghost" disabled={busy[server.id]} onClick={() => setForm({ mode: "edit", server })}>Edit</Button>
          <Button size="sm" variant="ghost" className="text-destructive-text" disabled={busy[server.id]} onClick={() => openDeleteDialog(server)}>Delete</Button>
        </div>
      ),
    },
  ];

  return (
    <>
      <PageHeader
        crumbs={[navCrumb("mcp", { current: true })]}
        titleRef={headingRef}
        titleProps={{ tabIndex: -1, className: "focus:outline-none" }}
        actions={
          <DropdownMenu open={addMenuOpen} onOpenChange={setAddMenuOpen}>
            <DropdownMenuTrigger asChild>
              <Button variant="primary" aria-keyshortcuts="c" data-testid="mcp-add-server">
                <Plus strokeWidth={ICON_STROKE_WIDTH} aria-hidden="true" />
                Add server
                <ChevronDown strokeWidth={ICON_STROKE_WIDTH} aria-hidden="true" />
                <Kbd className="hidden md:inline-flex">C</Kbd>
              </Button>
            </DropdownMenuTrigger>
            <DropdownMenuContent align="end">
              <DropdownMenuItem onSelect={openCatalog}>From the catalog</DropdownMenuItem>
              <DropdownMenuItem onSelect={openCreate}>Remote server by URL</DropdownMenuItem>
            </DropdownMenuContent>
          </DropdownMenu>
        }
      />
      <div className="mx-auto flex w-full min-w-0 max-w-7xl flex-col gap-4 px-4 py-6">
        <McpSectionTabs current="servers" />
        <p className="text-body text-muted-foreground">MCP servers that agents connect to: remote endpoints you register, and servers run here from the catalog. Test one and see the tools it offers.</p>

        {notice ? <p role="status" data-testid="mcp-notice" className="rounded-md border border-border bg-muted/40 p-3 text-body text-foreground">{notice}</p> : null}
        {actionError ? <p role="alert" data-testid="mcp-action-error" className="rounded-md border border-destructive/30 bg-destructive/10 p-3 text-body text-destructive-text">{actionError}</p> : null}

        <DataList
          label="MCP servers"
          testId="mcp-server-list"
          rows={servers}
          columns={columns}
          getRowId={(server) => server.id}
          getRowHref={(server) => routes.mcpServer(server.id)}
          getRowLabel={(server) => `${server.name}, ${server.enabled ? "enabled" : "disabled"}`}
          getRowTestId={() => SERVER_ROW.testId}
          searchPlaceholder="Search by name or URL"
          filterRow={(server, query) => {
            const q = query.toLowerCase();
            return server.name.toLowerCase().includes(q) || server.url.toLowerCase().includes(q);
          }}
          loading={loading}
          error={error}
          onRetry={() => void load()}
          empty={{
            title: "No servers yet. Install one from the Catalog or register a remote server.",
            action: (
              <div className="flex flex-wrap items-center justify-center gap-2">
                <Button variant="primary" asChild><Link href={routes.mcpBrowse()}>Install from the Catalog</Link></Button>
                <Button onClick={openCreate}>Register a remote server</Button>
              </div>
            ),
          }}
        />
      </div>

      {form ? (
        <McpServerDialog
          server={form.mode === "edit" ? form.server : undefined}
          onSaved={onSaved}
          onClose={() => setForm(null)}
        />
      ) : null}

      {confirmDelete ? (
        <McpDeleteConfirm
          server={confirmDelete}
          usageStatus={usage.status}
          usageRefreshing={usage.refreshing}
          usagePartial={usage.partial}
          usage={usage.usageFor(confirmDelete.name)}
          busy={busy[confirmDelete.id]}
          error={deleteError}
          onConfirm={() => void deleteServer(confirmDelete)}
          onCancel={() => openDeleteDialog(null)}
          // The deleted server's row (whose menu opened this) is gone: focus goes to the page title.
          fallbackFocus={() => headingRef.current}
        />
      ) : null}
    </>
  );
}
