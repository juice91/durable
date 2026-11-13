import React from "react";
import { cn } from "@/lib/cn";
import { describeMcpError, type McpServer } from "@/lib/mcpServers";

/** The status dot and words for a server's stored test result ("Connected", the error, or "Not tested"). */
export function McpLastTest({ server, showTime = true }: { server: McpServer; showTime?: boolean }) {
  const when = server.last_checked_at ? new Date(server.last_checked_at).toLocaleString() : null;
  const tone = server.last_status === "ok" ? "bg-success" : server.last_status === "error" ? "bg-destructive" : "bg-muted-foreground";
  return (
    <span className="flex min-w-0 items-start gap-2">
      <span aria-hidden="true" data-testid="mcp-status-dot" data-status={server.last_status ?? "untested"} className={cn("mt-1.5 size-2 shrink-0 rounded-full", tone)} />
      <span className="flex min-w-0 flex-col">
        {!server.last_status ? (
          <span className="text-muted-foreground">Not tested</span>
        ) : server.last_status === "ok" ? (
          <span className="text-success-text">Connected</span>
        ) : (
          <span className="text-destructive-text" title={server.last_error_code ?? undefined}>{describeMcpError(server.last_error_code)}</span>
        )}
        {showTime && when ? <span className="text-caption text-muted-foreground">{when}</span> : null}
      </span>
    </span>
  );
}
