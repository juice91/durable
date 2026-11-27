"use client";

import { usePermissions } from "@/lib/permissions";
import React, { useCallback, useEffect, useRef, useState } from "react";
import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import { McpDeleteConfirm } from "@/components/mcp/McpDeleteConfirm";
import { McpRunTab } from "@/components/mcp/McpRunTab";
import { McpLastTest } from "@/components/mcp/McpLastTest";
import { McpServerDialog } from "@/components/mcp/McpServerDialog";
import { McpTestResult } from "@/components/mcp/McpTestResult";
import { RuntimePanel } from "@/components/mcp/RuntimePanel";
import { McpToolSheet } from "@/components/mcp/McpToolSheet";
import { describeUsage, useMcpUsage } from "@/components/mcp/mcpUsage";
import { PermissionNotice } from "@/components/access/PermissionNotice";
import { ResourceAccessPanel } from "@/components/access/ResourceAccessPanel";
import { PageHeader } from "@/components/frame/PageHeader";
import { useAccessEnabled } from "@/lib/useIam";
import { DataList } from "@/components/shared/DataList";
import { EmptyState } from "@/components/shared/EmptyState";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/Tabs";
import { ApiError } from "@/lib/api";
import { ConflictNotice } from "@/components/shared/ConflictNotice";
import { useConflict } from "@/lib/useConflict";
import {
  describeMcpApiMessage,
  mcpServers,
  type McpDiscoveredTool,
  type McpServer,
  type McpTestResult as TestResult,
} from "@/lib/mcpServers";
import { navCrumb } from "@/lib/nav";
import { routes } from "@/lib/routes";

/** What "why can't I?" asks about on an MCP server page. */
const MCP_ACTIONS = ["run", "edit", "archive"] as const;

function errorMessage(reason: unknown, fallback: string): string {
  if (reason instanceof ApiError) return describeMcpApiMessage(reason.message);
  return reason instanceof Error ? reason.message : fallback;
}

const card = "min-w-0 rounded-lg border border-border bg-surface";

function Section({ title, testId, actions, children }: { title: string; testId: string; actions?: React.ReactNode; children: React.ReactNode }) {
  return (
    <section aria-labelledby={`${testId}-title`} data-testid={testId} className="flex min-w-0 flex-col gap-3">
      <div className="flex min-w-0 flex-wrap items-center justify-between gap-2">
        <h2 id={`${testId}-title`} className="text-title font-semibold text-foreground">{title}</h2>
        {actions ? <div className="flex flex-wrap items-center gap-2">{actions}</div> : null}
      </div>
      {children}
    </section>
  );
}

/**
 * One MCP server (a server we connect to): the Connection card with Test, the tools it offers (from
 * the last test run here), the agents that use it, and Delete with those references listed.
 */
export default function McpServerDetailPage() {
  const accessEnabled = useAccessEnabled();
  const params = useParams();
  const router = useRouter();
  const id = typeof params.id === "string" ? params.id : "";
  const [server, setServer] = useState<McpServer | null>(null);
  const [allServers, setAllServers] = useState<McpServer[]>([]);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [notFound, setNotFound] = useState(false);
  const [busy, setBusy] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  const [test, setTest] = useState<TestResult | null>(null);
  const [editing, setEditing] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [deleteError, setDeleteError] = useState<string | null>(null);
  const [openTool, setOpenTool] = useState<McpDiscoveredTool | null>(null);
  const usage = useMcpUsage();
  const deleting = useRef(false);

  const load = useCallback(async () => {
    if (!id) return;
    setLoadError(null);
    try {
      setServer(await mcpServers.get(id));
      setNotFound(false);
    } catch (reason: unknown) {
      if (reason instanceof ApiError && reason.status === 404) setNotFound(true);
      else setLoadError(errorMessage(reason, "Failed to load the MCP server"));
    }
  }, [id]);

  useEffect(() => {
    setServer(null);
    setTest(null);
    setOpenTool(null);
    void load();
  }, [load]);

  // The breadcrumb switcher's options; a failure only leaves the switcher with this server.
  useEffect(() => {
    let cancelled = false;
    mcpServers.list().then(
      (list) => {
        if (!cancelled) setAllServers(list);
      },
      () => undefined,
    );
    return () => {
      cancelled = true;
    };
  }, []);

  const permissions = usePermissions("mcp_server", server?.id, ["edit", "archive", "run"]);
  const mayEdit = permissions.can("edit");

  const withBusy = async (action: () => Promise<void>) => {
    setBusy(true);
    setActionError(null);
    try {
      await action();
    } catch (reason: unknown) {
      if (reason instanceof ApiError && reason.status === 404) setNotFound(true);
      else setActionError(errorMessage(reason, "The action failed"));
    } finally {
      setBusy(false);
    }
  };

  const testServer = (current: McpServer) => withBusy(async () => {
    const result = await mcpServers.test(current.id);
    setTest(result);
    // The test records last_status and last_checked_at.
    setServer(await mcpServers.get(current.id));
  });

  // 412: the server changed elsewhere; 重新載入 shows their version, 覆寫 resends the toggle on top of it.
  const conflict = useConflict<McpServer>({ onSaved: (saved) => setServer(saved as McpServer) });
  const toggleEnabled = (current: McpServer) => withBusy(async () => {
    const saved = await conflict.guard(() => mcpServers.setEnabled(current.id, !current.enabled));
    if (saved !== undefined) setServer(saved);
  });

  const onSaved = (saved: McpServer | null) => {
    setEditing(false);
    if (!saved || !server) return;
    // Changing the URL or headers clears the stored test result: the tool list is stale too.
    if (server.url !== saved.url || saved.last_status === null) {
      setTest(null);
      setOpenTool(null);
    }
    setServer(saved);
    // A rename changes which agent references resolve to this server.
    if (saved.name !== server.name) usage.reload();
  };

  const openDelete = () => {
    setDeleteError(null);
    setConfirmDelete(true);
    // Always re-check: an agent may have started using it since the page loaded. Delete waits for it.
    usage.recheck();
  };

  const deleteServer = async (current: McpServer) => {
    if (deleting.current) return;
    deleting.current = true;
    setBusy(true);
    setDeleteError(null);
    let leaving = false;
    try {
      await mcpServers.remove(current.id);
      // The confirm stays open (busy) until the navigation unmounts this page: closing it first would
      // return focus to the Delete button just before it disappears, leaving focus on <body>. Its
      // unmount then moves focus to the page's main.
      leaving = true;
      router.push(routes.mcp());
    } catch (reason: unknown) {
      if (reason instanceof ApiError && reason.status === 404) {
        setConfirmDelete(false);
        setNotFound(true);
      } else {
        setDeleteError(errorMessage(reason, "The server could not be deleted"));
      }
    } finally {
      if (!leaving) {
        deleting.current = false;
        setBusy(false);
      }
    }
  };

  const listCrumb = navCrumb("mcp");
  const switcherOptions = (allServers.length > 0 ? allServers : server ? [server] : [])
    .map((item) => ({ value: item.id, label: item.name }))
    .sort((a, b) => a.label.localeCompare(b.label));

  if (notFound) {
    return (
      <>
        <PageHeader crumbs={[listCrumb, { label: "Not found" }]} />
        <div className="mx-auto w-full min-w-0 max-w-3xl px-4 py-6">
          <EmptyState
            testId="mcp-server-not-found"
            title="MCP server not found"
            description="It may have been deleted, or the link is wrong."
            action={<Button asChild><Link href={routes.mcp()}>Back to MCP servers</Link></Button>}
          />
        </div>
      </>
    );
  }

  const tools = test?.status === "ok" ? test.tools : [];
  const used = server ? usage.usageFor(server.name) : [];

  return (
    <>
      <PageHeader
        crumbs={[
          listCrumb,
          {
            label: server?.name ?? "MCP server",
            switcher: server
              ? { label: "Switch MCP server", options: switcherOptions, value: server.id, hrefFor: routes.mcpServer, testId: "mcp-server-switcher" }
              : undefined,
          },
        ]}
        documentTitle={server?.name}
        actions={
          server && permissions.can("archive") ? (
            <Button variant="ghost" className="text-destructive-text" disabled={busy} onClick={openDelete} data-testid="mcp-delete">
              Delete
            </Button>
          ) : null
        }
      />
      <div className="mx-auto flex w-full min-w-0 max-w-3xl flex-col gap-8 px-4 py-6">
        {loadError ? (
          <div role="alert" className="flex flex-wrap items-center justify-between gap-3 rounded-lg border border-destructive/30 bg-destructive/10 p-3 text-body text-destructive-text">
            <span className="min-w-0">{loadError}</span>
            <Button size="sm" onClick={() => void load()}>Retry</Button>
          </div>
        ) : null}
        {!server && !loadError ? <p className="text-body text-muted-foreground" role="status">Loading MCP server…</p> : null}

        {server ? <PermissionNotice kind="mcp_server" id={server.id} actions={MCP_ACTIONS} /> : null}

        {server ? (
          <Tabs defaultValue="overview" className="flex min-w-0 flex-col gap-6">
            <TabsList aria-label="MCP server sections">
              <TabsTrigger value="overview" data-testid="mcp-tab-overview">Overview</TabsTrigger>
              <TabsTrigger value="run" data-testid="mcp-tab-run">Run</TabsTrigger>
              {server.runtime === "managed" ? <TabsTrigger value="runtime" data-testid="mcp-tab-runtime">Runtime</TabsTrigger> : null}
              {accessEnabled ? <TabsTrigger value="access" data-testid="mcp-tab-access">Access</TabsTrigger> : null}
            </TabsList>
            {accessEnabled ? (
              <TabsContent value="access">
                <ResourceAccessPanel kind="mcp_server" id={server.id} />
              </TabsContent>
            ) : null}
            {server.runtime === "managed" ? (
              <TabsContent value="runtime">
                <RuntimePanel serverId={server.id} enabled={server.enabled} onChanged={() => void load()} mayEdit={mayEdit} />
              </TabsContent>
            ) : null}
            <TabsContent value="run">
              <McpRunTab server={server} tools={tools} onLoadTools={() => void testServer(server)} loadingTools={busy} mayLoad={mayEdit} mayRun={permissions.can("run")} />
            </TabsContent>
            <TabsContent value="overview" className="flex min-w-0 flex-col gap-8">
            <Section
              title="Connection"
              testId="mcp-connection"
              actions={
                !mayEdit ? undefined : <>
                  <Button size="sm" disabled={busy} onClick={() => void toggleEnabled(server)}>{server.enabled ? "Disable" : "Enable"}</Button>
                  <Button size="sm" disabled={busy} onClick={() => setEditing(true)}>Edit</Button>
                  <Button size="sm" variant="primary" disabled={busy} onClick={() => void testServer(server)}>Test connection</Button>
                </>
              }
            >
              <div className={`${card} p-4`}>
                <dl className="grid min-w-0 grid-cols-1 gap-x-6 gap-y-3 text-body sm:grid-cols-[8rem_1fr]">
                  <dt className="text-label text-muted-foreground">URL</dt>
                  <dd className="min-w-0 break-all font-mono text-label text-foreground">{server.url}</dd>
                  <dt className="text-label text-muted-foreground">Transport</dt>
                  <dd className="text-foreground">Streamable HTTP</dd>
                  <dt className="text-label text-muted-foreground">Status</dt>
                  <dd><Badge variant={server.enabled ? "success" : "neutral"}>{server.enabled ? "Enabled" : "Disabled"}</Badge></dd>
                  <dt className="text-label text-muted-foreground">Headers</dt>
                  <dd className="min-w-0 text-foreground" data-testid="mcp-header-names">
                    {server.header_names.length > 0 ? server.header_names.map((name) => `${name} (set)`).join(", ") : <span className="text-muted-foreground">None</span>}
                  </dd>
                  <dt className="text-label text-muted-foreground">Last test</dt>
                  <dd className="min-w-0" data-testid="mcp-last-test"><McpLastTest server={server} /></dd>
                </dl>
                {busy ? <p className="mt-3 text-caption text-muted-foreground" aria-live="polite">Working…</p> : null}
                <ConflictNotice state={conflict} className="mt-3" onReload={(current) => current && setServer(current)} />
                {actionError ? <p role="alert" data-testid="mcp-action-error" className="mt-3 rounded-md border border-destructive/30 bg-destructive/10 p-3 text-body text-destructive-text">{actionError}</p> : null}
                {test ? <div className="mt-4 border-t border-border pt-4"><McpTestResult result={test} showTools={false} onRetry={mayEdit ? () => void testServer(server) : undefined} retrying={busy} /></div> : null}
              </div>
            </Section>

            <Section title="Available tools" testId="mcp-available-tools">
              <DataList
                label="Available tools"
                testId="mcp-tools-list"
                rows={tools}
                getRowId={(tool) => tool.name}
                getRowTestId={() => "mcp-tool"}
                onRowOpen={(tool) => setOpenTool(tool)}
                getRowLabel={(tool) => tool.name}
                selectedRowId={openTool?.name ?? null}
                loading={busy && !test}
                columns={[
                  { id: "name", header: "Name", sortable: true, sortValue: (tool) => tool.name, cell: (tool) => <span className="font-mono">{tool.name}</span> },
                  {
                    id: "description",
                    header: "Description",
                    cell: (tool) => <span className="line-clamp-2 text-muted-foreground">{tool.description || "—"}</span>,
                  },
                ]}
                empty={
                  test?.status === "ok"
                    ? { title: "No tools", description: "The server answered but offers no tools." }
                    : test?.status === "error"
                      ? { title: "No tools listed", description: "The last test failed; fix the connection and test again." }
                      : {
                          title: "Test the connection to list its tools",
                          description: "Tools are discovered from the server when you test it.",
                          action: mayEdit ? <Button variant="primary" disabled={busy} onClick={() => void testServer(server)}>Test connection</Button> : undefined,
                        }
                }
              />
            </Section>

            <Section title="Used by agents" testId="mcp-used-by">
              <div className={card} data-status={usage.status}>
                {usage.status === "loading" ? (
                  <p className="p-4 text-body text-muted-foreground">Checking which agents use it…</p>
                ) : usage.status === "error" ? (
                  <div className="flex flex-wrap items-center justify-between gap-3 p-4 text-body text-muted-foreground">
                    <span>Could not load agents.</span>
                    <Button size="sm" onClick={usage.reload}>Retry</Button>
                  </div>
                ) : used.length === 0 ? (
                  <p className="p-4 text-body text-muted-foreground">No agent uses its tools.</p>
                ) : (
                  <ul className="divide-y divide-border">
                    {used.map((item) => (
                      <li key={item.agentId} data-testid="mcp-used-by-agent" className="flex min-w-0 flex-wrap items-center justify-between gap-2 px-4 py-2">
                        <Link href={routes.agentEditor(item.agentId)} className="min-w-0 truncate font-medium text-foreground underline-offset-4 hover:underline">
                          {item.agentName}
                        </Link>
                        <span className="text-caption text-muted-foreground">{describeUsage(item)}</span>
                      </li>
                    ))}
                  </ul>
                )}
                {usage.status === "ready" && usage.partial ? (
                  <p className="border-t border-border px-4 py-2 text-caption text-muted-foreground">Some published versions could not be read; only their drafts were checked.</p>
                ) : null}
              </div>
            </Section>
            </TabsContent>
          </Tabs>
        ) : null}
      </div>

      {server && editing ? <McpServerDialog server={server} onSaved={onSaved} onClose={() => setEditing(false)} /> : null}

      {server && confirmDelete ? (
        <McpDeleteConfirm
          server={server}
          usageStatus={usage.status}
          usageRefreshing={usage.refreshing}
          usagePartial={usage.partial}
          usage={used}
          busy={busy}
          error={deleteError}
          onConfirm={() => void deleteServer(server)}
          onCancel={() => setConfirmDelete(false)}
        />
      ) : null}

      {server ? (
        <McpToolSheet
          tool={openTool}
          serverName={server.name}
          onClose={() => setOpenTool(null)}
          usedBy={openTool ? usage.usageForTool(server.name, openTool.name) : []}
          usageStatus={usage.status}
          usagePartial={usage.partial}
          onRetryUsage={usage.reload}
        />
      ) : null}
    </>
  );
}
