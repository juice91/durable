"use client";

import React, { useEffect, useState } from "react";
import type { McpToolRef } from "@/lib/api";
import { describeMcpError, mcpServers, mcpToolKey, type McpServer, type McpTestResult } from "@/lib/mcpServers";

const secondaryButton = "rounded-md border border-border px-3 py-1.5 text-sm text-card-foreground hover:bg-accent disabled:cursor-not-allowed disabled:opacity-50";

interface McpToolPickerProps {
  value: McpToolRef[];
  onChange: (value: McpToolRef[]) => void;
}

/**
 * Pick MCP tools per registered server. Tools are listed through the server's connection test
 * (on demand); when that fails the tool name can be typed instead. Publish checks the server.
 */
export function McpToolPicker({ value, onChange }: McpToolPickerProps) {
  const [servers, setServers] = useState<McpServer[] | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [discovered, setDiscovered] = useState<Record<string, McpTestResult>>({});
  const [loadingTools, setLoadingTools] = useState<Record<string, boolean>>({});
  const [typed, setTyped] = useState<Record<string, string>>({});

  useEffect(() => {
    let cancelled = false;
    mcpServers.list()
      .then((list) => { if (!cancelled) setServers(list); })
      .catch((error: unknown) => { if (!cancelled) setLoadError(error instanceof Error ? error.message : "Failed to load MCP servers"); });
    return () => { cancelled = true; };
  }, []);

  const selected = new Set(value.map(mcpToolKey));
  const toggle = (ref: McpToolRef) => {
    const key = mcpToolKey(ref);
    onChange(selected.has(key) ? value.filter((item) => mcpToolKey(item) !== key) : [...value, ref]);
  };

  const loadTools = async (server: McpServer) => {
    setLoadingTools((current) => ({ ...current, [server.id]: true }));
    try {
      const result = await mcpServers.test(server.id);
      setDiscovered((current) => ({ ...current, [server.id]: result }));
    } catch (error: unknown) {
      setDiscovered((current) => ({ ...current, [server.id]: { status: "error", error_code: error instanceof Error ? error.message : "connection_failed", tools: [] } }));
    } finally {
      setLoadingTools((current) => ({ ...current, [server.id]: false }));
    }
  };

  const addTyped = (server: McpServer) => {
    const tool = (typed[server.id] ?? "").trim();
    if (!tool) return;
    if (!selected.has(mcpToolKey({ server: server.name, tool }))) onChange([...value, { server: server.name, tool }]);
    setTyped((current) => ({ ...current, [server.id]: "" }));
  };

  const byName = new Map((servers ?? []).map((server) => [server.name, server]));
  const enabledServers = (servers ?? []).filter((server) => server.enabled);

  return (
    <div data-testid="mcp-tool-picker" className="space-y-3">
      <span className="block text-sm font-medium text-card-foreground">MCP tools</span>

      {value.length > 0 ? (
        <ul aria-label="Selected MCP tools" className="flex flex-wrap gap-2">
          {value.map((ref) => {
            const server = byName.get(ref.server);
            const warning = servers === null ? null : !server ? "server not registered" : !server.enabled ? "server disabled" : null;
            return (
              <li key={mcpToolKey(ref)} data-testid="mcp-selected-tool" className="inline-flex items-center gap-2 rounded-md border border-border bg-card px-2 py-1 text-sm">
                <span className="font-mono text-card-foreground">{ref.server}/{ref.tool}</span>
                {warning ? <span className="text-xs text-destructive">{warning}</span> : null}
                <button type="button" aria-label={`Remove ${ref.server}/${ref.tool}`} className="text-xs text-muted-foreground hover:text-card-foreground" onClick={() => toggle(ref)}>Remove</button>
              </li>
            );
          })}
        </ul>
      ) : null}

      {loadError ? <p className="text-xs text-muted-foreground">MCP servers could not be loaded: {loadError}</p> : null}
      {servers !== null && enabledServers.length === 0 ? <p className="text-xs text-muted-foreground">No enabled MCP servers. Register one on the MCP Servers page.</p> : null}

      {enabledServers.map((server) => {
        const result = discovered[server.id];
        return (
          <fieldset key={server.id} data-testid="mcp-picker-server" data-server-name={server.name} className="space-y-2 rounded-md border border-border p-3">
            <legend className="px-1 text-sm font-medium text-card-foreground">{server.name}</legend>
            <div className="flex flex-wrap items-center gap-2">
              <button type="button" className={secondaryButton} disabled={loadingTools[server.id]} onClick={() => void loadTools(server)}>
                {loadingTools[server.id] ? "Loading tools…" : result ? "Reload tools" : "Load tools"}
              </button>
              <label htmlFor={`mcp-tool-name-${server.id}`} className="sr-only">Tool name on {server.name}</label>
              <input
                id={`mcp-tool-name-${server.id}`}
                placeholder="or type a tool name"
                value={typed[server.id] ?? ""}
                onChange={(e) => setTyped((current) => ({ ...current, [server.id]: e.target.value }))}
                onKeyDown={(e) => { if (e.key === "Enter") { e.preventDefault(); addTyped(server); } }}
                className="rounded-md border border-border bg-card px-3 py-1.5 text-sm text-card-foreground"
              />
              <button type="button" className={secondaryButton} onClick={() => addTyped(server)}>Add</button>
            </div>
            {result?.status === "error" ? <p className="text-xs text-destructive">Could not list tools: {describeMcpError(result.error_code)} You can still type a tool name.</p> : null}
            {result?.status === "ok" ? (
              result.tools.length === 0 ? <p className="text-xs text-muted-foreground">This server offers no tools.</p> : (
                <div className="flex flex-wrap gap-2">
                  {result.tools.map((tool) => {
                    const ref = { server: server.name, tool: tool.name };
                    return (
                      <label key={tool.name} title={tool.description ?? undefined} className="inline-flex cursor-pointer items-center rounded-md border border-border bg-card px-3 py-2 hover:bg-accent">
                        <input type="checkbox" aria-label={`${server.name}/${tool.name}`} className="h-4 w-4 rounded border-border" checked={selected.has(mcpToolKey(ref))} onChange={() => toggle(ref)} />
                        <span className="ml-2 font-mono text-sm text-card-foreground">{tool.name}</span>
                      </label>
                    );
                  })}
                </div>
              )
            ) : null}
          </fieldset>
        );
      })}
    </div>
  );
}
