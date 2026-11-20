"use client";

import React, { useEffect, useState } from "react";
import Link from "next/link";
import { ApiError } from "@/lib/api";
import {
  buildHeadersPayload,
  describeMcpApiMessage,
  mcpServers,
  newRowKey,
  rowsFromServer,
  type HeaderRow,
  type McpServer,
  type McpServerUpdate,
} from "@/lib/mcpServers";
import { listSecrets, secretRef } from "@/lib/secrets";
import { routes } from "@/lib/routes";
import { ConflictNotice } from "@/components/shared/ConflictNotice";
import { useConflict } from "@/lib/useConflict";
import { Select } from "@/components/ui/Select";

const inputClass = "w-full rounded-md border border-input bg-background px-3 py-2 text-sm text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring";
const secondaryButton = "rounded-md border border-border px-3 py-1.5 text-sm text-foreground hover:bg-accent disabled:cursor-not-allowed disabled:opacity-50";

interface McpServerFormProps {
  /** The server being edited; absent when registering a new one. */
  server?: McpServer;
  /** The saved server as the API returned it, or null when an edit changed nothing (no request). */
  onSaved: (server: McpServer | null) => void;
  onCancel: () => void;
  /** Inside a Dialog that already shows the title: no card frame and no heading of its own. */
  embedded?: boolean;
}

/** Register or edit an MCP server. Header values are write-only and never prefilled. */
export function McpServerForm({ server, onSaved, onCancel, embedded = false }: McpServerFormProps) {
  const isCreate = !server;
  const [name, setName] = useState(server?.name ?? "");
  const [url, setUrl] = useState(server?.url ?? "");
  const [enabled, setEnabled] = useState(server?.enabled ?? true);
  const [rows, setRows] = useState<HeaderRow[]>(() => rowsFromServer(server?.header_names ?? []));
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // 412: the server changed elsewhere; 重新載入 refills the form from their version, 覆寫 saves on top of it.
  const conflict = useConflict<McpServer>({ onSaved: (saved) => onSaved(saved as McpServer) });
  // Stored secret names for the "use secret" pickers; a failed list just leaves no picker.
  const [secretNames, setSecretNames] = useState<string[]>([]);
  useEffect(() => {
    let live = true;
    listSecrets()
      .then((items) => live && setSecretNames(items.map((item) => item.name)))
      .catch(() => undefined);
    return () => {
      live = false;
    };
  }, []);

  const updateRow = (key: string, patch: Partial<HeaderRow>) =>
    setRows((current) => current.map((row) => (row.key === key ? ({ ...row, ...patch } as HeaderRow) : row)));

  const addRow = () => setRows((current) => [...current, { key: newRowKey(), origin: "new", name: "", value: "" }]);

  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    if (saving) return;
    const headers = buildHeadersPayload(rows, isCreate);
    if (headers.kind === "invalid") {
      setError(headers.message);
      return;
    }
    setSaving(true);
    setError(null);
    try {
      let saved: McpServer | null;
      if (isCreate) {
        saved = await mcpServers.create({
          name: name.trim(),
          url: url.trim(),
          enabled,
          ...(headers.kind === "set" ? { headers: headers.headers } : {}),
        });
      } else {
        const body: McpServerUpdate = {};
        if (name.trim() !== server.name) body.name = name.trim();
        if (url.trim() !== server.url) body.url = url.trim();
        if (headers.kind === "set") body.headers = headers.headers;
        // Nothing changed: send nothing and hand back nothing, so the list keeps its current row
        // (which may have changed since this form opened: enabled, last test, or deleted).
        if (Object.keys(body).length > 0) {
          const updated = await conflict.guard(() => mcpServers.update(server.id, body));
          if (updated === undefined) return; // held as a conflict notice
          saved = updated;
        } else {
          saved = null;
        }
      }
      onSaved(saved);
    } catch (reason: unknown) {
      if (reason instanceof ApiError) setError(describeMcpApiMessage(reason.message));
      else setError(reason instanceof Error ? reason.message : "Failed to save the server");
    } finally {
      setSaving(false);
    }
  };

  return (
    <form onSubmit={submit} aria-label={isCreate ? "Register MCP server" : `Edit MCP server ${server.name}`} data-testid="mcp-server-form" className={embedded ? "min-w-0 space-y-4" : "min-w-0 space-y-4 rounded-lg border border-border bg-card p-5"}>
      {embedded ? null : <h2 className="text-lg font-semibold text-card-foreground">{isCreate ? "Register MCP server" : `Edit ${server.name}`}</h2>}
      <ConflictNotice
        state={conflict}
        onReload={(current) => {
          if (!current) return;
          setName(current.name);
          setUrl(current.url);
          setEnabled(current.enabled);
          setRows(rowsFromServer(current.header_names));
        }}
      />
      {error ? <p role="alert" data-testid="mcp-form-error" className="rounded-md border border-destructive/30 bg-destructive/10 p-3 text-sm text-destructive-text">{error}</p> : null}
      {isCreate ? (
        <p data-testid="mcp-catalog-pointer" className="text-sm text-muted-foreground">
          Looking for a published server? Use the <Link href={routes.mcpBrowse()} className="font-medium text-primary underline-offset-4 hover:underline">Catalog tab</Link>.
        </p>
      ) : null}
      <div className="grid gap-4 md:grid-cols-2">
        <div className="space-y-1">
          <label htmlFor="mcp-name" className="block text-sm font-medium text-foreground">Name</label>
          <input id="mcp-name" required value={name} onChange={(e) => setName(e.target.value)} pattern="[A-Za-z0-9][A-Za-z0-9_.\-]{0,63}" className={inputClass} />
          <p className="text-xs text-muted-foreground">Letters, digits, <code>_ . -</code>. Agents reference tools by this name.</p>
        </div>
        <div className="space-y-1">
          <label htmlFor="mcp-url" className="block text-sm font-medium text-foreground">URL</label>
          <input id="mcp-url" required type="url" placeholder="https://mcp.example.com/mcp" value={url} onChange={(e) => setUrl(e.target.value)} className={inputClass} />
          <p className="text-xs text-muted-foreground">Streamable HTTP endpoint. Private and loopback addresses are refused.</p>
        </div>
      </div>
      {isCreate ? (
        <label className="flex items-center gap-2 text-sm text-foreground">
          <input type="checkbox" checked={enabled} onChange={(e) => setEnabled(e.target.checked)} className="h-4 w-4 rounded border-input" />
          Enabled
        </label>
      ) : null}

      <fieldset className="space-y-2" data-testid="mcp-headers">
        <legend className="text-sm font-medium text-foreground">Headers</legend>
        <p className="text-xs text-muted-foreground">Values are stored encrypted and are never shown again. A value of <code>secret:&lt;name&gt;</code> refers to a stored secret. Changing any header replaces the whole set.</p>
        {isCreate ? (
          <p data-testid="mcp-auth-header-hint" className="text-xs text-muted-foreground">Add your own <code>Authorization</code> header (for example <code>Bearer …</code>); the registry never carries credentials.</p>
        ) : null}
        {rows.map((row, index) => (
          <div key={row.key} data-testid="mcp-header-row" className="flex flex-wrap items-center gap-2">
            {row.origin === "existing" ? (
              <>
                <span className={`min-w-40 font-mono text-sm ${row.state === "removed" ? "text-muted-foreground line-through" : "text-foreground"}`}>{row.name}</span>
                {row.state === "kept" ? <span data-testid="mcp-header-set" className="text-sm text-muted-foreground">•••• set</span> : null}
                {row.state === "replace" ? (
                  <>
                    <label htmlFor={`header-value-${row.key}`} className="sr-only">New value for {row.name}</label>
                    <input id={`header-value-${row.key}`} type="password" autoComplete="off" placeholder="New value" value={row.value} onChange={(e) => updateRow(row.key, { value: e.target.value })} className={`${inputClass} max-w-sm`} />
                    {secretNames.length > 0 ? (
                      <>
                        <label htmlFor={`header-secret-${row.key}`} className="sr-only">New value for {row.name} secret</label>
                        <Select id={`header-secret-${row.key}`} value="" onValueChange={(v) => v && updateRow(row.key, { value: secretRef(v) })} className={`${inputClass} max-w-40`}>
                          <option value="">Use secret…</option>
                          {secretNames.map((secretName) => <option key={secretName} value={secretName}>{secretName}</option>)}
                        </Select>
                      </>
                    ) : null}
                  </>
                ) : null}
                {row.state === "removed" ? <span className="text-sm text-muted-foreground">will be removed</span> : null}
                {row.state === "kept" ? <button type="button" aria-label={`Replace ${row.name}`} className={secondaryButton} onClick={() => updateRow(row.key, { state: "replace", value: "" })}>Replace</button> : null}
                {row.state !== "kept" ? <button type="button" aria-label={`Undo changes to ${row.name}`} className={secondaryButton} onClick={() => updateRow(row.key, { state: "kept", value: "" })}>Undo</button> : null}
                {row.state !== "removed" ? <button type="button" aria-label={`Remove ${row.name}`} className={secondaryButton} onClick={() => updateRow(row.key, { state: "removed", value: "" })}>Remove</button> : null}
              </>
            ) : (
              <>
                <label htmlFor={`header-name-${row.key}`} className="sr-only">Header {index + 1} name</label>
                <input id={`header-name-${row.key}`} placeholder="Header name, e.g. Authorization" value={row.name} onChange={(e) => updateRow(row.key, { name: e.target.value })} className={`${inputClass} max-w-xs font-mono`} />
                <label htmlFor={`header-value-${row.key}`} className="sr-only">Header {index + 1} value</label>
                <input id={`header-value-${row.key}`} type="password" autoComplete="off" placeholder="Secret value" value={row.value} onChange={(e) => updateRow(row.key, { value: e.target.value })} className={`${inputClass} max-w-sm`} />
                {secretNames.length > 0 ? (
                      <>
                        <label htmlFor={`header-secret-${row.key}`} className="sr-only">Header {index + 1} secret</label>
                        <Select id={`header-secret-${row.key}`} value="" onValueChange={(v) => v && updateRow(row.key, { value: secretRef(v) })} className={`${inputClass} max-w-40`}>
                          <option value="">Use secret…</option>
                          {secretNames.map((secretName) => <option key={secretName} value={secretName}>{secretName}</option>)}
                        </Select>
                      </>
                    ) : null}
                <button type="button" aria-label={`Remove header ${index + 1}`} className={secondaryButton} onClick={() => setRows((current) => current.filter((item) => item.key !== row.key))}>Remove</button>
              </>
            )}
          </div>
        ))}
        <button type="button" className={secondaryButton} onClick={addRow}>Add header</button>
      </fieldset>

      <div className="flex gap-2 border-t border-border pt-4">
        <button type="submit" disabled={saving} className="rounded-md bg-primary px-4 py-2 text-sm font-medium text-primary-foreground disabled:cursor-not-allowed disabled:opacity-50">{isCreate ? "Register" : "Save"}</button>
        <button type="button" className={secondaryButton} onClick={onCancel}>Cancel</button>
      </div>
    </form>
  );
}
