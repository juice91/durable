"use client";

import React from "react";
import { claudeCodeMcpSnippet, codexMcpSnippet, mcpEndpoint } from "@/lib/apiKeys";
import { CopyField } from "./CopyField";

/**
 * "Connect an MCP client": the endpoint and the connect snippets for Claude Code and Codex. The
 * snippets name the key by an environment variable, never by value, so nothing here is secret.
 * Every key of the tenant may read through MCP; a key with the Manage scope may also change things.
 */
export function McpConnectPanel() {
  return (
    <section aria-labelledby="settings-mcp-connect" data-testid="settings-mcp-connect" className="flex min-w-0 flex-col gap-3 border-t border-border pt-3">
      <div className="flex min-w-0 flex-col gap-1">
        <h3 id="settings-mcp-connect" className="text-label font-semibold text-foreground">
          Connect an MCP client
        </h3>
        <p className="text-label text-muted-foreground">
          An MCP client (Claude Code, Codex, another agent) can manage this workspace with a key: every key can read agents, workflows, runs and knowledge bases;
          a key created with <span className="font-medium text-foreground">Allow changes</span> can also publish, run, sync and decide approvals. Secrets are never
          exposed.
        </p>
      </div>
      <CopyField label="MCP endpoint" value={mcpEndpoint()} testId="mcp-endpoint" copiedMessage="Endpoint copied" />
      <CopyField label="Claude Code" value={claudeCodeMcpSnippet()} multiline testId="mcp-snippet-claude" copiedMessage="Snippet copied" />
      <CopyField label="Codex" value={codexMcpSnippet()} multiline testId="mcp-snippet-codex" copiedMessage="Snippet copied" />
    </section>
  );
}
