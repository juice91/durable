"use client";

import React from "react";
import { describeMcpError, isOutboundPolicyCode, type McpTestResult as Result } from "@/lib/mcpServers";

/** The outcome of a connection test: the discovered tools, or the error in words. */
export function McpTestResult({ result }: { result: Result }) {
  if (result.status === "error") {
    return (
      <div role="alert" data-testid="mcp-test-error" className="rounded-md border border-destructive/30 bg-destructive/10 p-3 text-sm text-destructive">
        <p className="font-semibold">{isOutboundPolicyCode(result.error_code) ? "Blocked by the outbound policy" : "Connection failed"}</p>
        <p>{describeMcpError(result.error_code)}</p>
        <p className="mt-1 text-xs">Error code: <code className="font-mono">{result.error_code}</code></p>
      </div>
    );
  }
  return (
    <div data-testid="mcp-test-tools" className="space-y-2">
      <p className="text-sm text-foreground">Connected. {result.tools.length} tool{result.tools.length === 1 ? "" : "s"} discovered.</p>
      <ul className="space-y-2">
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
      </ul>
    </div>
  );
}
