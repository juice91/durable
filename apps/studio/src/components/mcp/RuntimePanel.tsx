"use client";

import React, { useCallback, useEffect, useRef, useState } from "react";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { ApiError } from "@/lib/api";
import { mcpServers, runtimeActionsFor, type McpRuntime, type McpRuntimeAction, type McpRuntimeState } from "@/lib/mcpServers";

export const RUNTIME_STATE_LABEL: Record<McpRuntimeState, string> = {
  pending: "Pending",
  starting: "Starting",
  running: "Running",
  idle: "Idle",
  stopping: "Stopping",
  stopped: "Stopped",
  failed: "Failed",
};

export function runtimeStateVariant(state: McpRuntimeState): "success" | "info" | "warning" | "destructive" | "neutral" {
  switch (state) {
    case "running": return "success";
    case "idle": return "neutral";
    case "pending":
    case "starting": return "info";
    case "stopping": return "warning";
    case "stopped": return "neutral";
    case "failed": return "destructive";
  }
}

/** The state chip shared by the list rows and the runtime panel. */
export function RuntimeStateChip({ state, testId }: { state: McpRuntimeState; testId?: string }) {
  return <Badge variant={runtimeStateVariant(state)} data-testid={testId}>{RUNTIME_STATE_LABEL[state]}</Badge>;
}

const TAILS = [100, 200, 500, 1000] as const;

function message(reason: unknown, fallback: string): string {
  if (reason instanceof ApiError) return reason.message;
  return reason instanceof Error ? reason.message : fallback;
}

interface RuntimePanelProps {
  serverId: string;
  /** A disabled server refuses start and restart. */
  enabled: boolean;
  /** Called after a start, stop or restart so the page can reload the server row. */
  onChanged: () => void;
}

export function RuntimePanel({ serverId, enabled, onChanged }: RuntimePanelProps) {
  const [runtime, setRuntime] = useState<McpRuntime | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [acting, setActing] = useState<McpRuntimeAction | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [tail, setTail] = useState<number>(200);
  const [lines, setLines] = useState<string[] | null>(null);
  const [logsError, setLogsError] = useState<string | null>(null);
  const [logsLoading, setLogsLoading] = useState(false);
  const logsRequest = useRef(0);

  const loadRuntime = useCallback(async () => {
    try {
      setRuntime(await mcpServers.runtime(serverId));
      setLoadError(null);
    } catch (reason: unknown) {
      setLoadError(message(reason, "The runtime state could not be loaded"));
    }
  }, [serverId]);

  const loadLogs = useCallback(async () => {
    const id = ++logsRequest.current;
    setLogsLoading(true);
    try {
      const result = await mcpServers.runtimeLogs(serverId, tail);
      if (id !== logsRequest.current) return;
      setLines(result.lines);
      setLogsError(null);
    } catch (reason: unknown) {
      if (id !== logsRequest.current) return;
      setLines(null);
      setLogsError(reason instanceof ApiError && reason.status === 502 ? "Cluster unreachable" : message(reason, "The logs could not be loaded"));
    } finally {
      if (id === logsRequest.current) setLogsLoading(false);
    }
  }, [serverId, tail]);

  useEffect(() => {
    void loadRuntime();
  }, [loadRuntime]);
  useEffect(() => {
    void loadLogs();
  }, [loadLogs]);

  const act = async (action: McpRuntimeAction) => {
    setActing(action);
    setActionError(null);
    try {
      setRuntime(await mcpServers.runtimeAction(serverId, action));
      onChanged();
      void loadLogs();
    } catch (reason: unknown) {
      setActionError(message(reason, "The action failed"));
    } finally {
      setActing(null);
    }
  };

  const allowed = runtime ? runtimeActionsFor(runtime.state) : { start: false, stop: false, restart: false };
  const idle = acting === null;
  const card = "min-w-0 rounded-lg border border-border bg-surface p-4";

  return (
    <div className="flex min-w-0 flex-col gap-6" data-testid="mcp-runtime-panel">
      <section aria-labelledby="runtime-title" className="flex min-w-0 flex-col gap-3">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <h2 id="runtime-title" className="text-title font-semibold text-foreground">Runtime</h2>
          <div className="flex flex-wrap items-center gap-2">
            <Button size="sm" disabled={!idle || !runtime || !allowed.start || !enabled} onClick={() => void act("start")}>Start</Button>
            <Button size="sm" disabled={!idle || !runtime || !allowed.stop} onClick={() => void act("stop")}>Stop</Button>
            <Button size="sm" disabled={!idle || !runtime || !allowed.restart || !enabled} onClick={() => void act("restart")}>Restart</Button>
          </div>
        </div>
        <div className={card}>
          {loadError ? (
            <div role="alert" className="flex flex-wrap items-center justify-between gap-3 text-body text-destructive-text">
              <span className="min-w-0">{loadError}</span>
              <Button size="sm" onClick={() => void loadRuntime()}>Retry</Button>
            </div>
          ) : !runtime ? (
            <p role="status" className="text-body text-muted-foreground">Loading the runtime…</p>
          ) : (
            <dl className="grid min-w-0 grid-cols-1 gap-x-6 gap-y-3 text-body sm:grid-cols-[8rem_1fr]">
              <dt className="text-label text-muted-foreground">State</dt>
              <dd><RuntimeStateChip state={runtime.state} testId="runtime-state" /></dd>
              {runtime.message ? (<><dt className="text-label text-muted-foreground">Message</dt><dd className="min-w-0 break-words text-foreground">{runtime.message}</dd></>) : null}
              {runtime.last_error ? (<><dt className="text-label text-muted-foreground">Last error</dt><dd className="min-w-0 break-words text-destructive-text">{runtime.last_error}</dd></>) : null}
              <dt className="text-label text-muted-foreground">Reachable</dt>
              <dd className="text-foreground">{runtime.reachable ? "Yes" : "No"}</dd>
              {runtime.package ? (<><dt className="text-label text-muted-foreground">Package</dt><dd className="min-w-0 break-all font-mono text-label text-foreground">{runtime.package.registry_type}: {runtime.package.identifier}@{runtime.package.version}</dd></>) : null}
              {runtime.image ? (<><dt className="text-label text-muted-foreground">Image</dt><dd className="min-w-0 break-all font-mono text-label text-foreground">{runtime.image}</dd></>) : null}
              {runtime.last_used_at ? (<><dt className="text-label text-muted-foreground">Last used</dt><dd className="text-foreground">{new Date(runtime.last_used_at).toLocaleString()}</dd></>) : null}
            </dl>
          )}
          {!enabled ? <p className="mt-3 text-caption text-muted-foreground">The server is disabled: enable it to start or restart.</p> : null}
          {acting ? <p className="mt-3 text-caption text-muted-foreground" aria-live="polite">Working…</p> : null}
          {actionError ? <p role="alert" className="mt-3 rounded-md border border-destructive/30 bg-destructive/10 p-3 text-body text-destructive-text">{actionError}</p> : null}
        </div>
      </section>

      <section aria-labelledby="runtime-logs-title" className="flex min-w-0 flex-col gap-3">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <h2 id="runtime-logs-title" className="text-title font-semibold text-foreground">Logs</h2>
          <div className="flex flex-wrap items-center gap-2">
            <label htmlFor="runtime-log-tail" className="text-label text-muted-foreground">Lines</label>
            <select
              id="runtime-log-tail"
              value={tail}
              onChange={(event) => setTail(Number(event.target.value))}
              className="rounded-md border border-input bg-background px-2 py-1 text-body text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            >
              {TAILS.map((value) => <option key={value} value={value}>{value}</option>)}
            </select>
            <Button size="sm" disabled={logsLoading} onClick={() => void loadLogs()}>Refresh</Button>
          </div>
        </div>
        {logsError ? (
          <p role="alert" className="rounded-md border border-destructive/30 bg-destructive/10 p-3 text-body text-destructive-text">{logsError}</p>
        ) : lines === null ? (
          <p role="status" className="text-body text-muted-foreground">Loading logs…</p>
        ) : lines.length === 0 ? (
          <p className="text-body text-muted-foreground">No log lines yet.</p>
        ) : (
          <pre tabIndex={0} aria-label="Runtime logs" data-testid="runtime-logs" className="max-h-96 min-w-0 overflow-auto whitespace-pre-wrap break-all rounded-lg border border-border bg-muted/40 p-3 font-mono text-caption text-foreground">{lines.join("\n")}</pre>
        )}
      </section>
    </div>
  );
}
