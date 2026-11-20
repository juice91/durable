"use client";

import React from "react";
import { Button } from "@/components/ui/Button";
import { describeMcpError, describeStartedTest, isOutboundPolicyCode, type McpTestResult as Result } from "@/lib/mcpServers";

/** The outcome of a connection test: the discovered tools, or the error in words. */
export function McpTestResult({ result, showTools = true, onRetry, retrying = false }: {
  result: Result;
  /** List the tools here (false when the page lists them itself). */
  showTools?: boolean;
  /** Re-run the test (offered while the server is still starting). */
  onRetry?: () => void;
  retrying?: boolean;
}) {
  if (result.status === "error" && result.error_code === "runtime_starting") {
    return (
      <div role="status" data-testid="mcp-test-starting" className="flex flex-wrap items-center justify-between gap-3 rounded-md border border-border bg-muted/40 p-3 text-sm text-foreground">
        <p>Server is starting, test again in a moment</p>
        {onRetry ? <Button size="sm" disabled={retrying} onClick={onRetry} data-testid="mcp-test-retry">Retry</Button> : null}
      </div>
    );
  }
  if (result.status === "error") {
    return (
      <div role="alert" data-testid="mcp-test-error" className="rounded-md border border-destructive/30 bg-destructive/10 p-3 text-sm text-destructive-text">
        <p className="font-semibold">{isOutboundPolicyCode(result.error_code) ? "Blocked by the outbound policy" : "Connection failed"}</p>
        <p>{describeMcpError(result.error_code)}</p>
        <p className="mt-1 text-xs">Error code: <code className="font-mono">{result.error_code}</code></p>
      </div>
    );
  }
  return (
    <div data-testid="mcp-test-tools" className="space-y-2">
      {describeStartedTest(result.runtime) ? <p data-testid="mcp-test-started" className="text-sm font-medium text-success-text">{describeStartedTest(result.runtime)}</p> : null}
      <p className="text-sm text-foreground">Connected. {result.tools.length} tool{result.tools.length === 1 ? "" : "s"} discovered.</p>
      {showTools ? <ul className="space-y-2">
        {result.tools.map((tool) => (
          <li key={tool.name} data-testid="mcp-tool" className="rounded-md border border-border bg-background p-3">
            <p className="font-mono text-sm font-medium text-foreground">{tool.name}</p>
            {tool.description ? <p className="mt-1 text-sm text-muted-foreground">{tool.description}</p> : null}
            <details className="mt-2">
              <summary className="cursor-pointer text-xs text-muted-foreground">Input schema</summary>
              <pre className="mt-2 max-h-64 overflow-auto rounded-md bg-muted p-3 text-xs text-foreground">{JSON.stringify(tool.input_schema, null, 2)}</pre>
            </details>
          </li>
        ))}
      </ul> : null}
    </div>
  );
}
