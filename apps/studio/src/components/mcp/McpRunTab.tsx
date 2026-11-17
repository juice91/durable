"use client";

import React, { useCallback, useEffect, useMemo, useState } from "react";
import Link from "next/link";
import { StatusPill } from "@/components/runs/StatusPill";
import { ToolRunner } from "@/components/tools/ToolRunner";
import { toolRunDurationMs } from "@/components/tools/ToolRunResult";
import { Button } from "@/components/ui/Button";
import { formatDuration } from "@/lib/format";
import { listMcpToolRuns, mcpToolDefinition, startMcpToolRun } from "@/lib/mcpToolRuns";
import type { McpDiscoveredTool, McpServer } from "@/lib/mcpServers";
import { routes } from "@/lib/routes";
import type { Run } from "@/lib/runs";

interface McpRunTabProps {
  server: McpServer;
  /** The tools from the last connection test; empty until the server has been tested. */
  tools: McpDiscoveredTool[];
  /** Run the connection test, which lists the tools. */
  onLoadTools: () => void;
  loadingTools: boolean;
}

/**
 * Run tab of an MCP server: pick one of its tools, run it with the shared tool runner and see its
 * result and its recent runs. Runs are recorded on the tool-run ledger like the built-in tools'.
 */
export function McpRunTab({ server, tools, onLoadTools, loadingTools }: McpRunTabProps) {
  const [selected, setSelected] = useState<string>("");
  const [history, setHistory] = useState<Run[] | null>(null);
  const [historyError, setHistoryError] = useState<string | null>(null);
  const tool = useMemo(() => tools.find((item) => item.name === selected) ?? null, [tools, selected]);

  useEffect(() => {
    if (selected && !tools.some((item) => item.name === selected)) setSelected("");
  }, [tools, selected]);

  const loadHistory = useCallback(async (name: string) => {
    setHistoryError(null);
    try {
      setHistory(await listMcpToolRuns(server.id, name));
    } catch (reason: unknown) {
      setHistoryError(reason instanceof Error ? reason.message : "Failed to load the run history");
    }
  }, [server.id]);

  useEffect(() => {
    setHistory(null);
    if (selected) void loadHistory(selected);
  }, [selected, loadHistory]);

  if (tools.length === 0) {
    return (
      <div data-testid="mcp-run-empty" className="flex flex-col items-start gap-3 rounded-lg border border-border bg-surface p-4">
        <p className="text-body text-muted-foreground">
          {server.enabled ? "Test the connection to list the tools you can run." : "This server is disabled. Enable it, then test the connection to run its tools."}
        </p>
        <Button variant="primary" disabled={loadingTools || !server.enabled} onClick={onLoadTools}>Test connection</Button>
      </div>
    );
  }

  return (
    <div className="flex min-w-0 flex-col gap-6" data-testid="mcp-run-tab">
      <div className="flex min-w-0 flex-col gap-1">
        <label htmlFor="mcp-run-tool" className="text-label font-semibold text-foreground">Tool</label>
        <select
          id="mcp-run-tool"
          value={selected}
          onChange={(event) => setSelected(event.target.value)}
          className="h-9 w-full max-w-sm rounded-md border border-input bg-background px-2 font-mono text-body text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
        >
          <option value="">Select a tool</option>
          {tools.map((item) => <option key={item.name} value={item.name}>{item.name}</option>)}
        </select>
        {tool?.description ? <p className="text-caption text-muted-foreground">{tool.description}</p> : null}
      </div>

      {tool ? (
        <>
          <ToolRunner
            key={`${server.id}/${tool.name}`}
            tool={mcpToolDefinition(server.id, tool)}
            start={(input, runId) => startMcpToolRun(server.id, tool.name, input, runId)}
            onStarted={() => void loadHistory(tool.name)}
          />
          <section aria-labelledby="mcp-run-history-title" data-testid="mcp-run-history" className="flex min-w-0 flex-col gap-2">
            <h3 id="mcp-run-history-title" className="text-label font-semibold text-foreground">Recent runs</h3>
            {historyError ? (
              <div role="alert" className="flex flex-wrap items-center justify-between gap-3 text-body text-destructive-text">
                <span>{historyError}</span>
                <Button size="sm" onClick={() => void loadHistory(tool.name)}>Retry</Button>
              </div>
            ) : history === null ? (
              <p className="text-body text-muted-foreground" role="status">Loading runs…</p>
            ) : history.length === 0 ? (
              <p className="text-body text-muted-foreground">No runs of this tool yet.</p>
            ) : (
              <ul className="divide-y divide-border rounded-lg border border-border bg-surface">
                {history.map((run) => (
                  <li key={run.run_id} data-testid="mcp-run-history-row" className="flex min-w-0 flex-wrap items-center gap-3 px-4 py-2">
                    <StatusPill status={run.display_status} />
                    <span className="text-caption text-muted-foreground">{formatDuration(toolRunDurationMs(run))}</span>
                    <time className="text-caption text-muted-foreground" dateTime={run.created_at}>{new Date(run.created_at).toLocaleString()}</time>
                    <Link href={routes.run(run.run_id)} className="ml-auto text-label text-foreground underline underline-offset-4 hover:no-underline">Open run</Link>
                  </li>
                ))}
              </ul>
            )}
          </section>
        </>
      ) : null}
    </div>
  );
}
