"use client";

import React, { useCallback, useEffect, useRef, useState } from "react";
import { McpServerForm } from "@/components/mcp/McpServerForm";
import { McpTestResult } from "@/components/mcp/McpTestResult";
import { ConfirmDialog } from "@/components/shared/ConfirmDialog";
import { ApiError } from "@/lib/api";
import { describeMcpApiMessage, describeMcpError, mcpServers, type McpServer, type McpTestResult as TestResult } from "@/lib/mcpServers";
import { PageHeader } from "@/components/frame/PageHeader";
import { navCrumb } from "@/lib/nav";

const secondaryButton = "rounded-md border border-border px-3 py-1.5 text-sm text-foreground hover:bg-accent disabled:cursor-not-allowed disabled:opacity-50";

type FormState = { mode: "create" } | { mode: "edit"; server: McpServer } | null;

function errorMessage(reason: unknown, fallback: string): string {
  if (reason instanceof ApiError) return describeMcpApiMessage(reason.message);
  return reason instanceof Error ? reason.message : fallback;
}

function LastTest({ server }: { server: McpServer }) {
  if (!server.last_status) return <span className="text-muted-foreground">Not tested</span>;
  const when = server.last_checked_at ? new Date(server.last_checked_at).toLocaleString() : null;
  return (
    <span className="flex flex-col">
      {server.last_status === "ok"
        ? <span className="text-primary">Connected</span>
        : <span className="text-destructive-text" title={server.last_error_code ?? undefined}>{describeMcpError(server.last_error_code)}</span>}
      {when ? <span className="text-xs text-muted-foreground">{when}</span> : null}
    </span>
  );
}

export default function McpServersPage() {
  const [servers, setServers] = useState<McpServer[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [form, setForm] = useState<FormState>(null);
  const [busy, setBusy] = useState<Record<string, boolean>>({});
  const [tests, setTests] = useState<Record<string, TestResult>>({});
  const [confirmDelete, setConfirmDelete] = useState<McpServer | null>(null);
  const [deleteError, setDeleteError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  // The server whose delete dialog is open right now (state is stale inside async callbacks).
  const openDeleteId = useRef<string | null>(null);
  // After a delete the button that opened the dialog is gone; focus lands on the page heading.
  const headingRef = useRef<HTMLHeadingElement>(null);

  const load = useCallback(async () => {
    try {
      setServers(await mcpServers.list());
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

  const replaceServer = (server: McpServer) =>
    setServers((current) => {
      const exists = current.some((item) => item.id === server.id);
      const next = exists ? current.map((item) => (item.id === server.id ? server : item)) : [...current, server];
      return next.sort((a, b) => a.name.localeCompare(b.name));
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
  };

  const forgetServer = (id: string) => {
    // A deleted server's edit form has nothing left to save.
    setForm((current) => (current?.mode === "edit" && current.server.id === id ? null : current));
    setServers((current) => current.filter((item) => item.id !== id));
    setTests((current) => {
      const next = { ...current };
      delete next[id];
      return next;
    });
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
    // Changing the URL or headers clears the stored test result; drop the stale tool list too.
    if (editing && (editing.url !== server.url || server.last_status === null)) {
      setTests((current) => {
        const next = { ...current };
        delete next[server.id];
        return next;
      });
    }
    if (editing) {
      // An edit never re-adds a row that was deleted meanwhile.
      setServers((current) => current.map((item) => (item.id === server.id ? server : item)));
    } else {
      replaceServer(server);
    }
  };

  return (
    <>
      <PageHeader
        crumbs={[navCrumb("mcp", { current: true })]}
        titleRef={headingRef}
        titleProps={{ tabIndex: -1, className: "focus:outline-none" }}
        actions={
          form === null ? (
            <button type="button" onClick={() => setForm({ mode: "create" })} className="rounded-md bg-primary px-3 py-1.5 text-sm font-medium text-primary-foreground">
              Register server
            </button>
          ) : null
        }
      />
    <div className="mx-auto max-w-7xl px-4 py-8">
      <div className="mb-6 flex flex-wrap items-start justify-between gap-4">
        <div>
          <p className="mt-1 text-muted-foreground">Register remote MCP servers, test them and see the tools they offer to agents.</p>
        </div>
      </div>

      {form ? (
        <div className="mb-6">
          <McpServerForm
            key={form.mode === "edit" ? form.server.id : "create"}
            server={form.mode === "edit" ? form.server : undefined}
            onSaved={onSaved}
            onCancel={() => setForm(null)}
          />
        </div>
      ) : null}

      {error ? <p role="alert" className="rounded-md border border-destructive/30 bg-destructive/10 p-3 text-sm text-destructive-text">{error}</p> : null}
      {notice ? <p role="status" data-testid="mcp-notice" className="mb-4 rounded-md border border-border bg-muted/40 p-3 text-sm text-foreground">{notice}</p> : null}
      {actionError ? <p role="alert" data-testid="mcp-action-error" className="mb-4 rounded-md border border-destructive/30 bg-destructive/10 p-3 text-sm text-destructive-text">{actionError}</p> : null}
      {loading ? <p className="text-muted-foreground">Loading MCP servers…</p> : null}

      {!loading && !error && servers.length === 0 ? (
        <div className="rounded-lg border border-dashed border-border p-10 text-center">
          <p className="font-medium text-foreground">No MCP servers yet</p>
          <p className="mt-1 text-sm text-muted-foreground">Register a remote server over Streamable HTTP to make its tools available to agents.</p>
        </div>
      ) : null}

      {!loading && servers.length > 0 ? (
        <ul aria-label="MCP servers" className="space-y-3">
          {servers.map((server) => (
            <li key={server.id} data-testid="mcp-server-row" data-server-name={server.name} className="rounded-lg border border-border bg-card p-4">
              <div className="flex flex-wrap items-start justify-between gap-4">
                <div className="min-w-0 space-y-1">
                  <div className="flex items-center gap-2">
                    <h2 className="font-semibold text-card-foreground">{server.name}</h2>
                    <span className={`rounded-full px-2 py-0.5 text-xs ${server.enabled ? "bg-primary/10 text-primary" : "bg-muted text-muted-foreground"}`}>{server.enabled ? "Enabled" : "Disabled"}</span>
                  </div>
                  <p className="break-all font-mono text-xs text-muted-foreground">{server.url}</p>
                  {server.header_names.length > 0 ? <p className="text-xs text-muted-foreground">Headers: {server.header_names.map((name) => `${name} (set)`).join(", ")}</p> : null}
                  <div className="text-sm" data-testid="mcp-last-test"><LastTest server={server} /></div>
                </div>
                <div className="flex flex-wrap gap-2">
                  <button type="button" className={secondaryButton} disabled={busy[server.id]} onClick={() => void testServer(server)}>Test connection</button>
                  <button type="button" className={secondaryButton} disabled={busy[server.id]} onClick={() => void toggleEnabled(server)}>{server.enabled ? "Disable" : "Enable"}</button>
                  <button type="button" className={secondaryButton} disabled={busy[server.id]} onClick={() => setForm({ mode: "edit", server })}>Edit</button>
                  <button type="button" className={`${secondaryButton} text-destructive-text`} disabled={busy[server.id]} onClick={() => openDeleteDialog(server)}>Delete</button>
                </div>
              </div>
              {busy[server.id] ? <p className="mt-2 text-xs text-muted-foreground" aria-live="polite">Working…</p> : null}
              {tests[server.id] ? <div className="mt-4 border-t border-border pt-4"><McpTestResult result={tests[server.id]} /></div> : null}
            </li>
          ))}
        </ul>
      ) : null}

      {confirmDelete ? (
        <ConfirmDialog
          title={`Delete ${confirmDelete.name}?`}
          description="Its stored header secrets are deleted too. Agents that use its tools will fail to publish or run until they are changed."
          confirmLabel="Delete server"
          destructive
          busy={busy[confirmDelete.id]}
          onConfirm={() => void deleteServer(confirmDelete)}
          error={deleteError}
          onCancel={() => openDeleteDialog(null)}
        />
      ) : null}
    </div>
    </>
  );
}
